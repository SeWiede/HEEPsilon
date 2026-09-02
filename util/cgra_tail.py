#!/usr/bin/env python3
"""cgra_tail.py — the shared half of the CGRA kernel toolchain.

Everything downstream of the front end lives here. A front end's only job is to
produce LLVM IR whose target loop carries the tag

    !llvm.loop !0
    !0 = distinct !{!0, !1}
    !1 = !{!"llvm.loop.cgra.acc"}

after which `drive_tail()` takes it the rest of the way:

    opt -passes=cgra-extract  →  SAT-MapIt mapper
                              →  satmapit_parse.py  →  cgra_gen.py

Two front ends use this (see docs/cgra_kernel_toolchain.md, docs/cgra_glsl_route.md):

    util/cgra_satmap.py   C with #pragma cgra acc  — clang emits the tag
    util/cgra_glsl.py     GLSL compute shader      — SPIR-V + MLIR, tag injected

This module is not a CLI. Import it.
"""

import os
import re
import shutil
import subprocess
import sys

HEEPSILON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SATMAPIT_DIR = os.environ.get(
    "SATMAPIT_DIR",
    os.path.join(os.path.dirname(HEEPSILON_ROOT), "SAT-MapIt")
)

CLANG_REL  = "llvm-project/build/bin/clang"
OPT_REL    = "llvm-project/build/bin/opt"
MAPPER_REL = "mapper/main.py"
PYTHON_REL = "cgra-compiler/bin/python"

# The tag cgra-extract selects on — CGRAExtract.cpp::hasPragmaCGRAAcc().
TAG = "llvm.loop.cgra.acc"

# Markers for instructions/patterns SAT-MapIt's own tooling silently fails on
# internally (exit code 0) rather than raising an error -- e.g. an LLVM intrinsic
# call (min/max, etc.) that CGRAExtract.cpp's instructionSelection() has no case
# for, which leaves a DFG node's opcode at its unset default (-1) and surfaces
# only as "UNDEF" several stages later in cgra_gen.py. Catch them here, right at
# the source, instead of letting them propagate into a confusing crash further
# down the pipeline.
#
# The last three surface when driving non-clang IR; "Unconditional jumps" is
# printed just before a bare exit(0) (CGRAExtract.cpp:865).
KNOWN_FAILURE_MARKERS = [
    "Instruction not supported or not defined in the ISA",
    "No assignment for instuction",
    "unsupported instruciton",
    "UNDEF",
    "Instruction Selection will be disabled",
    "Unconditional jumps not supported",
    "More than 3 operands for inst",
]


def grid_from_cgra_header():
    """(n_row, n_col) of the built hardware, from the generated driver header."""
    path = os.path.join(HEEPSILON_ROOT, "sw", "external", "drivers", "cgra", "cgra.h")
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return 4, 4
    def _get(macro, dflt):
        m = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", text, re.MULTILINE)
        return int(m.group(1)) if m else dflt
    return _get("CGRA_N_ROWS", 4), _get("CGRA_N_COLS", 4)


def check_for_known_failures(text, desc):
    if not text:
        return
    for marker in KNOWN_FAILURE_MARKERS:
        if marker in text:
            matching_lines = "\n".join(l for l in text.splitlines() if marker in l)
            sys.exit(
                f"ERROR: {desc} reported an unhandled instruction/pattern "
                f"(exit code was 0, but this is a known silent-failure marker):\n"
                f"{matching_lines}\n"
                f"See TODO_satmapit_toolchain.md section 0 for background -- "
                f"this usually means the input contains a pattern (e.g. an "
                f"LLVM intrinsic call like min/max) that SAT-MapIt's extraction "
                f"pass has no instruction-selection case for."
            )


def run(cmd, cwd, desc, env=None):
    print(f"  [{desc}] {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        print(f"  STDERR:\n{result.stderr}")
        sys.exit(f"ERROR: {desc} failed (exit {result.returncode})")
    check_for_known_failures(result.stderr, desc)
    check_for_known_failures(result.stdout, desc)
    return result.stdout


def find_acc_dirs(d):
    return sorted(x for x in os.listdir(d)
                  if x.startswith("acc") and os.path.isdir(os.path.join(d, x)))


class Steps:
    """`[k/n] ...` progress lines, so both front ends number their own stages
    and the shared tail continues the same count."""

    def __init__(self, total):
        self.total, self.n = total, 0

    def __call__(self, msg, blank_line=False):
        self.n += 1
        print(f"{chr(10) if blank_line else ''}[{self.n}/{self.total}] {msg}",
              flush=True)


def check_satmapit(satmapit_dir, need_clang=False):
    """Verify the SAT-MapIt checkout has everything the tail needs."""
    parts = [("SAT-MapIt dir", satmapit_dir),
             ("opt",           os.path.join(satmapit_dir, OPT_REL)),
             ("mapper",        os.path.join(satmapit_dir, MAPPER_REL)),
             ("venv python",   os.path.join(satmapit_dir, PYTHON_REL))]
    if need_clang:
        parts.insert(1, ("clang", os.path.join(satmapit_dir, CLANG_REL)))
    for label, path in parts:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found: {path}\n"
                     f"       Run setup.sh in the SAT-MapIt repo first, "
                     f"or pass --satmapit-dir.")


def drive_tail(ir_file, *, work_dir, satmapit_dir, name, n_row, n_col, out_dir,
               app=None, ref_src=None, gen_extra=(), steps=None):
    """Tagged LLVM IR → generated app. Everything after the front end.

    ir_file    LLVM IR (.ll/.bc) with the target loop carrying TAG
    work_dir   where acc*/ and cgra-code-* are written (the pass emits acc*
               into its cwd, and the mapper reads them relative to it)
    app        None → stop after instructions_*.py and return its path

    Returns the path to the generated main.c, or to the kernel spec if app is
    None.
    """
    step = steps or Steps(3 if app else 2)
    opt    = os.path.join(satmapit_dir, OPT_REL)
    python = os.path.join(satmapit_dir, PYTHON_REL)
    mapper = os.path.join(satmapit_dir, MAPPER_REL)

    # ── CGRA extraction ───────────────────────────────────────────────────────
    step("Running CGRA extraction pass (opt) ...")
    for d in find_acc_dirs(work_dir):
        shutil.rmtree(os.path.join(work_dir, d))
    run([opt, "-disable-output", ir_file, "-passes=cgra-extract"],
        cwd=work_dir, desc="opt")

    acc_dirs = find_acc_dirs(work_dir)
    if not acc_dirs:
        sys.exit("ERROR: opt pass produced no acc* directories — check that the "
                 f"input has a loop tagged !{{!\"{TAG}\"}} in its !llvm.loop "
                 "metadata (from #pragma cgra acc, or injected by the front end).")
    print(f"  Found {len(acc_dirs)} loop(s): {acc_dirs}")

    # ── SAT-MapIt mapper ──────────────────────────────────────────────────────
    step(f"Running SAT-MapIt mapper (x={n_row}, y={n_col}) ...")
    acc_code_files = []
    for acc_dir in acc_dirs:
        out_file = os.path.join(work_dir, f"cgra-code-{acc_dir}")
        result = subprocess.run(
            [python, mapper, "-path", f"{acc_dir}/",
             "-x", str(n_row), "-y", str(n_col), "-r", "10", "-no_assembly", "1"],
            cwd=work_dir, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  STDERR:\n{result.stderr}")
            sys.exit(f"ERROR: mapper failed for {acc_dir} (exit {result.returncode})")
        check_for_known_failures(result.stderr, f"mapper ({acc_dir})")
        check_for_known_failures(result.stdout, f"mapper ({acc_dir})")
        with open(out_file, "w") as fout:
            fout.write(result.stdout)
        acc_code_files.append(out_file)
        print(f"  Mapped {acc_dir} → {os.path.basename(out_file)}")
        shutil.rmtree(os.path.join(work_dir, acc_dir))

    if len(acc_code_files) > 1:
        print(f"  WARNING: {len(acc_code_files)} loops found — only processing the first one.")
        print(f"           For the others, run satmapit_parse.py manually:")
        for f in acc_code_files[1:]:
            print(f"           python util/satmapit_parse.py {f} --name ...")

    # ── satmapit_parse.py → instructions_*.py ─────────────────────────────────
    here     = os.path.dirname(os.path.abspath(__file__))
    parse_py = os.path.join(here, "satmapit_parse.py")
    gen_py   = os.path.join(here, "cgra_gen.py")
    out_dir  = os.path.abspath(out_dir)
    acc_file = acc_code_files[0]

    if app:
        step(f"Generating instructions + app '{app}' ...", blank_line=True)
    else:
        step("done — generating instructions file ...", blank_line=True)

    # With --app the schedule belongs inside the app's own directory: it is what
    # the generated summary's TODOs tell you to consult, and leaving it loose in
    # sw/satmapit/ just accumulates files next to the app dirs. Without --app
    # there is no app dir yet, so it stays at the top level.
    draft_dir = os.path.join(out_dir, app) if app else out_dir
    os.makedirs(draft_dir, exist_ok=True)

    if subprocess.run([sys.executable, parse_py, acc_file, "--name", name,
                       "--n-row", str(n_row), "--n-col", str(n_col),
                       "--out-dir", draft_dir, "--pipeline"]).returncode != 0:
        sys.exit("ERROR: satmapit_parse.py failed")

    draft = os.path.join(draft_dir, name + ".py")
    if not app:
        print(f"\nNext: python util/cgra_gen.py {draft} <app_name>")
        return draft

    # ── cgra_gen.py → runnable app ────────────────────────────────────────────
    # Anything the front end didn't recognise is forwarded verbatim to
    # cgra_gen.py, so its flags (--rotate-cols, --sweep, --sweep-range,
    # --sweep-trials, ...) are reachable from the entry point people actually
    # use. --ref-src supplies only the host-side verification oracle and the
    # sweep's trip count; it has no effect on the CGRA bitstream.
    cmd = [sys.executable, gen_py, draft, app, "--out-dir", out_dir]
    if ref_src:
        cmd += ["--ref-src", os.path.abspath(ref_src)]
    if subprocess.run(cmd + list(gen_extra)).returncode != 0:
        sys.exit("ERROR: cgra_gen.py failed")

    main_c = os.path.join(out_dir, app, "main.c")
    print(f"\n{'='*60}")
    print(f"Done: {main_c}")
    print(f"{'='*60}")
    # NOTE: cgra_gen.py just printed its own end-of-run summary above (assumptions,
    # column-role inference, anything needing manual review) — read that, not a
    # canned claim here, since whether edits are needed varies per kernel.
    print(f"""
Simulate:  python run.py --simulator verilator --tests {app}
On FPGA:   python run.py --board zcu104 --tests {app}
""")
    return main_c
