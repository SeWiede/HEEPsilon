"""cgra_glsl.py — GLSL compute shader → runnable CGRA app.

    glslc → spirv-opt → MLIR → LLVM IR → opt → SAT-MapIt mapper
          → satmapit_parse.py → cgra_gen.py

Shares everything from the opt stage on with util/cgra_satmap.py; see
util/cgra_tail.py.

Usage:
    python util/cgra_glsl.py <shader.comp.glsl> --app <app_name>
    python util/cgra_glsl.py <shader.comp.glsl>     # stop at instructions_*.py

A pre-compiled .spv may be given instead of .glsl.

The shader must be integer-only (the CGRA has no floating point),
single-invocation, with one counted innermost loop, and use storage buffers
with fixed-size arrays. Unsupported constructs are rejected before any work
starts. Constraints, pipeline and limits: docs/cgra_glsl_route.md.

Configuration
-------------
SATMAPIT_DIR   path to the SAT-MapIt checkout   (default: ../SAT-MapIt)
MLIR_DIR       LLVM 20 install with mlir-opt/mlir-translate
               (default: /usr/lib/llvm-20 — `apt install mlir-20-tools`)
glslc and spirv-opt are taken from PATH (Vulkan SDK).
"""

import sys
import os
import re
import argparse
import subprocess
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgra_tail as tail
from cgra_tail import run, TAG as _TAG

_DEFAULT_MLIR_DIR = os.environ.get("MLIR_DIR", "/usr/lib/llvm-20")

# Canonicalisation into the shape cgra-extract needs. Two orderings are
# load-bearing and fail silently when wrong:
#   simplifycfg before loop-rotate — SPIR-V's loop header is `br label %cond`,
#     not an exiting block, so rotation has nothing to do and cgra-extract then
#     hits exit(0) on the unconditional latch.
#   indvars after it — cgra-extract emits the icmp's predicate without checking
#     which successor is the loop body, so a shader's `i < N` maps inverted
#     unless LFTR rewrites the test to `icmp eq`.
_CANON_PASSES = ("sroa,instcombine,simplifycfg,loop-simplify,loop-rotate,"
                 "loop-mssa(licm),indvars,simplifycfg,instcombine")

# 32-bit target. MLIR emits no datalayout, so LLVM would assume 64-bit pointers
# and inject sext into the loop body — an extra node the ISA has no case for.
_DATALAYOUT = ('target datalayout = "e-m:e-p:32:32-i64:64-n32-S128"\n'
               'target triple = "riscv32-unknown-elf"\n')

_DEF_N_ROW, _DEF_N_COL = tail.grid_from_cgra_header()


# ── Shader-side checks ────────────────────────────────────────────────────────

def check_shader(path):
    """Reject up front what MLIR or the CGRA cannot handle, with the reason."""
    with open(path) as fh:
        src = fh.read()
    body = re.sub(r"//.*|/\*.*?\*/", "", src, flags=re.S)   # ignore comments

    problems = []
    if re.search(r"\bpush_constant\b", body):
        problems.append("push constants — PushConstant storage class is rejected by "
                        "--convert-spirv-to-llvm; put the values in a storage buffer")
    if re.search(r"\buniform\b\s+\w+\s*\{", body):
        problems.append("uniform block — use a `buffer` (storage) block instead")
    if re.search(r"\b(readonly|writeonly)\b", body):
        problems.append("readonly/writeonly — they emit NonWritable/NonReadable member "
                        "decorations, and convertStructType() bails on any of those")
    if re.search(r"\w+\s+\w+\s*\[\s*\]\s*;", body):
        problems.append("runtime array (`T name[];`) — spirv.rtarray does not convert; "
                        "give the array a fixed size")
    if re.search(r"\b(float|vec[234]|mat[234])\b", body):
        problems.append("floating point — the CGRA is integer/fixed-point (FAdd selects "
                        "FXP_ADD and the ISA has no IEEE-754), so a float kernel would "
                        "map, run, and return nonsense")
    if problems:
        sys.exit("ERROR: shader uses constructs this route cannot handle:\n"
                 + "".join(f"  - {p}\n" for p in problems)
                 + "See the module docstring for a shader that works.")


# ── SPIR-V → LLVM IR ──────────────────────────────────────────────────────────

def _mlir_env(mlir_dir):
    """mlir-* need libMLIR.so on the loader path; the apt package does not
    register it with ldconfig."""
    env = dict(os.environ)
    lib = os.path.join(mlir_dir, "lib")
    env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def _unwrap_module(text):
    """--convert-spirv-to-llvm emits `module { module { ... } }`, and
    --mlir-to-llvmir translates only the outer one, silently producing an empty
    .ll. Drop the outer wrapper."""
    lines = text.rstrip("\n").split("\n")
    if not lines[0].strip().startswith("module"):
        return text
    return "\n".join(l[2:] if l.startswith("  ") else l for l in lines[1:-1]) + "\n"


def _prep_for_cgra(text):
    """Add the 32-bit datalayout and flatten access-chain GEPs.

    SPIR-V's Block struct wrapper is a formality (member 0 sits at offset 0), but
    it makes OpAccessChain lower to a 3-index GEP — 4 LLVM operands — and
    cgra-extract disables instruction selection for the whole graph on anything
    with more than 3 (CGRAExtract.cpp:338). No stock LLVM pass flattens it,
    because struct-typed GEPs are kept for alias analysis.
    """
    text = text.replace('source_filename = "LLVMDialectModule"\n',
                        'source_filename = "LLVMDialectModule"\n\n' + _DATALAYOUT + "\n", 1)
    text, n = re.subn(
        r"getelementptr \{ \[\d+ x i32\] \}, ptr (@?[\w.]+), i32 0, i32 0, i32 (%?[\w.]+)",
        r"getelementptr i32, ptr \1, i32 \2", text)
    print(f"  flattened {n} access-chain GEP(s)")
    return text


def _tag_loop(text):
    """Attach the CGRA tag to the innermost loop.

    MLIR emits no loop metadata at all, so there is nothing to hang the tag on:
    synthesise a loop ID on the latch of the single-block loop that
    canonicalisation produced. A single-block loop's terminator names its own
    block as a branch target, which is unambiguous in textual IR.
    """
    nums = [int(n) for n in re.findall(r"^!(\d+) = ", text, re.MULTILINE)]
    loop_id, tag_id = max(nums, default=-1) + 1, max(nums, default=-1) + 2

    out, block, tagged = [], None, False
    for line in text.split("\n"):
        m = re.match(r"^([\w.$-]+):", line)
        if m:
            block = m.group(1)
        elif re.match(r"^\S", line):
            block = None
        if (not tagged and block and re.match(r"\s*br\b", line)
                and re.search(rf"label %{re.escape(block)}\b", line)):
            line = line.rstrip() + f", !llvm.loop !{loop_id}"
            tagged = True
        out.append(line)
    if not tagged:
        sys.exit("ERROR: no single-block loop found to tag. The shader's loop did "
                 "not canonicalise into the form cgra-extract needs — check that "
                 "it is a simple counted innermost loop.")
    return ("\n".join(out).rstrip("\n")
            + f"\n!{loop_id} = distinct !{{!{loop_id}, !{tag_id}}}"
            + f'\n!{tag_id} = !{{!"{_TAG}"}}\n')


def shader_to_tagged_ir(source, work, mlir_dir, opt, steps):
    """glsl/spv → canonicalised, tagged .ll. Returns the path."""
    env = _mlir_env(mlir_dir)
    mlir_translate = os.path.join(mlir_dir, "bin", "mlir-translate")
    mlir_opt       = os.path.join(mlir_dir, "bin", "mlir-opt")
    p = lambda name: os.path.join(work, name)

    if source.endswith(".spv"):
        spv = source
        steps("Using pre-built SPIR-V (glslc skipped) ...", blank_line=True)
    else:
        steps("Compiling shader with glslc ...", blank_line=True)
        spv = p("shader.spv")
        # vulkan1.1 => SPIR-V 1.3 `Block` + StorageBuffer, not SPIR-V 1.0's
        # legacy BufferBlock + Uniform, which the converter rejects.
        run(["glslc", "-fshader-stage=comp", "--target-env=vulkan1.1",
             source, "-o", spv], cwd=work, desc="glslc")

    steps("SPIR-V → LLVM IR (MLIR) ...")
    # MLIR turns *named* SPIR-V structs into identified structs, and
    # convertStructType() only handles literal ones. glslang always names
    # interface blocks, so strip OpName.
    run(["spirv-opt", "--strip-debug", spv, "-o", p("stripped.spv")],
        cwd=work, desc="spirv-opt")
    run([mlir_translate, "--deserialize-spirv", p("stripped.spv"),
         "-o", p("spirv.mlir")], cwd=work, desc="mlir-translate", env=env)

    # glslang's `int` is OpTypeInt 32 1 → MLIR si32, but llvm.icmp/llvm.add
    # require signless i32. Safe to rewrite: SPIR-V carries signedness in the
    # ops (SLessThan vs ULessThan), not the types.
    with open(p("spirv.mlir")) as fh:
        signless = re.sub(r"\bsi32\b", "i32", fh.read())
    with open(p("signless.mlir"), "w") as fh:
        fh.write(signless)

    run([mlir_opt, "--convert-spirv-to-llvm", p("signless.mlir"),
         "-o", p("llvm.mlir")], cwd=work, desc="mlir-opt", env=env)
    with open(p("llvm.mlir")) as fh:
        flat = _unwrap_module(fh.read())
    with open(p("llvm.flat.mlir"), "w") as fh:
        fh.write(flat)
    run([mlir_translate, "--mlir-to-llvmir", p("llvm.flat.mlir"), "-o", p("raw.ll")],
        cwd=work, desc="mlir-translate", env=env)

    with open(p("raw.ll")) as fh:
        raw = fh.read()
    if "define" not in raw:
        sys.exit("ERROR: mlir-to-llvmir produced an empty module. This usually "
                 "means --convert-spirv-to-llvm silently dropped the function.")
    with open(p("prep.ll"), "w") as fh:
        fh.write(_prep_for_cgra(raw))

    steps("Canonicalising the loop ...")
    run([opt, "-S", p("prep.ll"), f"-passes={_CANON_PASSES}", "-o", p("canon.ll")],
        cwd=work, desc="opt (canonicalise)")
    with open(p("canon.ll")) as fh:
        tagged = _tag_loop(fh.read())
    with open(p("tagged.ll"), "w") as fh:
        fh.write(tagged)
    print(f"  tagged the loop with !{{!\"{_TAG}\"}}")
    return p("tagged.ll")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",         help="GLSL compute shader (.comp.glsl) or .spv")
    ap.add_argument("--name",         default=None,
                    help="Output name stem (default: instructions_<source basename>)")
    ap.add_argument("--n-row",        type=int, default=_DEF_N_ROW,
                    help=f"HEEPsilon N_ROW — SAT-MapIt -x value (default {_DEF_N_ROW})")
    ap.add_argument("--n-col",        type=int, default=_DEF_N_COL,
                    help=f"HEEPsilon N_COL — SAT-MapIt -y value (default {_DEF_N_COL})")
    ap.add_argument("--app",          default=None,
                    help="Generate sw/satmapit/<app>/main.c (complete, runnable)")
    ap.add_argument("--ref-src",      default=None,
                    help="C file with a #pragma cgra acc loop computing the same "
                         "result, used ONLY for the host-side verification oracle "
                         "and to size the sweep. Without it the app still builds, "
                         "but verify.c is a stub and the sweep bound is guessed.")
    ap.add_argument("--satmapit-dir", default=None,
                    help=f"Path to SAT-MapIt repo (default: {tail.DEFAULT_SATMAPIT_DIR})")
    ap.add_argument("--mlir-dir",     default=None,
                    help=f"LLVM 20 install with mlir-opt/mlir-translate "
                         f"(default: {_DEFAULT_MLIR_DIR})")
    ap.add_argument("--keep-ir",      default=None, metavar="DIR",
                    help="Copy every intermediate (.spv, .mlir, .ll) here")
    ap.add_argument("--out-dir",      default="sw/satmapit",
                    help="Output directory for instructions_*.py and app/")
    args, gen_extra = ap.parse_known_args()

    satmapit_dir = os.path.abspath(args.satmapit_dir or tail.DEFAULT_SATMAPIT_DIR)
    mlir_dir     = os.path.abspath(args.mlir_dir or _DEFAULT_MLIR_DIR)
    source_abs   = os.path.abspath(args.source)

    tail.check_satmapit(satmapit_dir)
    if not os.path.exists(source_abs):
        sys.exit(f"ERROR: shader not found: {source_abs}")
    for label, path in [("mlir-opt",       os.path.join(mlir_dir, "bin", "mlir-opt")),
                        ("mlir-translate", os.path.join(mlir_dir, "bin", "mlir-translate"))]:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found: {path}\n"
                     f"       sudo apt install mlir-20-tools, or pass --mlir-dir.")

    for tool in ("glslc", "spirv-opt"):
        if shutil.which(tool) is None:
            sys.exit(f"ERROR: {tool} not on PATH — source the Vulkan SDK setup script "
                     f"(e.g. `. ~/tools/vulkansdk/<ver>/setup-env.sh`).")

    # The binaries can exist but not start: the apt package does not register
    # libMLIR.so with ldconfig. Check they run, not just that they are there.
    probe = subprocess.run([os.path.join(mlir_dir, "bin", "mlir-opt"), "--version"],
                           capture_output=True, text=True, env=_mlir_env(mlir_dir))
    if probe.returncode != 0:
        sys.exit(f"ERROR: mlir-opt is present but will not run:\n"
                 f"       {probe.stderr.strip().splitlines()[-1] if probe.stderr else ''}\n"
                 f"       The tools package needs its library: "
                 f"sudo apt install libmlir-20 mlir-20-tools")

    # cgra_gen.py parses the --ref-src C loop with clang.cindex to find the trip
    # count. Without it that inference silently falls back and the generated sweep
    # can hang in simulation, so fail here instead. Only matters with --ref-src.
    if args.ref_src:
        try:
            import clang.cindex  # noqa: F401
        except ImportError:
            sys.exit("ERROR: python module 'clang' (clang.cindex) is missing, which "
                     "cgra_gen.py needs to read the --ref-src loop bound. Without it "
                     "the trip count is guessed and the generated app can hang.\n"
                     "       conda activate core-v-mini-mcu")

    opt = os.path.join(satmapit_dir, tail.OPT_REL)

    base = re.sub(r"\.(comp\.)?(glsl|spv)$", "", os.path.basename(source_abs))
    name = args.name or f"instructions_{base}"
    steps = tail.Steps(6 if args.app else 5)

    print(f"=== cgra_glsl: {base} → {args.app or name} ===")
    print(f"SAT-MapIt dir : {satmapit_dir}")
    print(f"MLIR dir      : {mlir_dir}")
    print(f"Shader        : {source_abs}")
    print(f"CGRA size     : {args.n_row} rows × {args.n_col} cols  "
          f"(-x {args.n_row} -y {args.n_col})")

    if not source_abs.endswith(".spv"):
        check_shader(source_abs)

    work = tempfile.mkdtemp(prefix="cgra_glsl_")
    try:
        # ── The front end: shader → LLVM IR with the loop tagged by hand ──────
        ir = shader_to_tagged_ir(source_abs, work, mlir_dir, opt, steps)

        # ── The shared tail, identical to the one cgra_satmap.py drives ───────
        if not args.ref_src:
            print("  NOTE: no --ref-src — verify.c will be a stub and the sweep "
                  "bound is guessed. The bitstream is unaffected.")
        tail.drive_tail(ir, work_dir=work, satmapit_dir=satmapit_dir,
                        name=name, n_row=args.n_row, n_col=args.n_col,
                        out_dir=os.path.abspath(args.out_dir), app=args.app,
                        ref_src=args.ref_src, gen_extra=gen_extra, steps=steps)
    finally:
        if args.keep_ir:
            os.makedirs(args.keep_ir, exist_ok=True)
            for f in sorted(os.listdir(work)):
                src = os.path.join(work, f)
                if os.path.isfile(src):
                    shutil.copy2(src, args.keep_ir)
            print(f"Intermediates: {args.keep_ir}")
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
