#!/usr/bin/env python3
"""cgra_satmap.py — full pipeline: C source → ready-to-run CGRA app in one command.

Invokes clang → opt → SAT-MapIt mapper → satmapit_parse.py → cgra_gen.py.

Usage:
    python util/cgra_satmap.py <source.c> --app <app_name>

Example:
    python util/cgra_satmap.py benchmarks/vec_sum/vec_sum.c --app cgra_vec_sum
    python run.py --simulator verilator --tests cgra_vec_sum
    python run.py --board zcu104 --tests cgra_vec_sum

    # Without --app: stops after generating instructions_*.py only
    python util/cgra_satmap.py my_kernel.c

Output (with --app):
    sw/satmapit/<app>/main.c  — complete test app, no manual edits needed

Everything is auto-generated:
    - CGRA bitstream (KMEM + CMEM arrays)
    - Buffer declarations with correct sizes (computed from section breakdown)
    - Stream pointer setup (which cols get read/write pointers, from CMEM scan)
    - Reference function (extracted from C source, pragma stripped, _ref suffix)
    - Test data fill + PASS/FAIL comparison to reference function

Transforms applied automatically by satmapit_parse.py:
    SMUL → NOP, LWI → LWD, address SADDs → NOP, phi nodes classified,
    extra epilog drain steps removed.  See util/satmapit_parse.py for details.

Configuration
-------------
Set SATMAPIT_DIR to the path of your SAT-MapIt checkout, or pass --satmapit-dir.
Default: ../SAT-MapIt  (sibling directory next to the HEEPsilon project root)
"""

import sys
import os
import re
import argparse
import subprocess
import shutil

_HEEPSILON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SATMAPIT_DIR = os.environ.get(
    "SATMAPIT_DIR",
    os.path.join(os.path.dirname(_HEEPSILON_ROOT), "SAT-MapIt")
)

_CLANG_REL  = "llvm-project/build/bin/clang"
_OPT_REL    = "llvm-project/build/bin/opt"
_MAPPER_REL = "mapper/main.py"
_PYTHON_REL = "cgra-compiler/bin/python"


def run(cmd, cwd, desc):
    print(f"  [{desc}] {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  STDERR:\n{result.stderr}")
        sys.exit(f"ERROR: {desc} failed (exit {result.returncode})")
    return result.stdout


def find_acc_dirs(satmapit_dir):
    return sorted(
        d for d in os.listdir(satmapit_dir)
        if d.startswith("acc") and os.path.isdir(os.path.join(satmapit_dir, d))
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",         help="C source file containing #pragma cgra acc loop")
    ap.add_argument("--name",         default=None,
                    help="Output name stem, e.g. 'instructions_vec_sum' "
                         "(default: instructions_<source basename>)")
    ap.add_argument("--n-row",        type=int, default=4,
                    help="HEEPsilon N_ROW — SAT-MapIt -x value (default 4)")
    ap.add_argument("--n-col",        type=int, default=4,
                    help="HEEPsilon N_COL — SAT-MapIt -y value (default 4)")
    ap.add_argument("--app",          default=None,
                    help="Generate sw/satmapit/<app>/main.c (complete, runnable)")
    ap.add_argument("--satmapit-dir", default=None,
                    help=f"Path to SAT-MapIt repo (default: {_DEFAULT_SATMAPIT_DIR})")
    ap.add_argument("--out-dir",      default="sw/satmapit",
                    help="Output directory for instructions_*.py and app/ (default: sw/satmapit)")
    args = ap.parse_args()

    satmapit_dir = os.path.abspath(args.satmapit_dir or _DEFAULT_SATMAPIT_DIR)
    source_abs   = os.path.abspath(args.source)

    for label, path in [
        ("SAT-MapIt dir", satmapit_dir),
        ("clang",         os.path.join(satmapit_dir, _CLANG_REL)),
        ("opt",           os.path.join(satmapit_dir, _OPT_REL)),
        ("mapper",        os.path.join(satmapit_dir, _MAPPER_REL)),
        ("venv python",   os.path.join(satmapit_dir, _PYTHON_REL)),
        ("source file",   source_abs),
    ]:
        if not os.path.exists(path):
            sys.exit(f"ERROR: {label} not found: {path}\n"
                     f"       Run setup.sh in the SAT-MapIt repo first, or pass --satmapit-dir.")

    clang  = os.path.join(satmapit_dir, _CLANG_REL)
    opt    = os.path.join(satmapit_dir, _OPT_REL)
    python = os.path.join(satmapit_dir, _PYTHON_REL)
    mapper = os.path.join(satmapit_dir, _MAPPER_REL)

    base = os.path.splitext(os.path.basename(source_abs))[0]
    name = args.name or f"instructions_{base}"
    n_steps = 4 if args.app else 3

    print(f"=== cgra_satmap: {base} → {args.app or name} ===")
    print(f"SAT-MapIt dir : {satmapit_dir}")
    print(f"Source        : {source_abs}")
    print(f"CGRA size     : {args.n_row} rows × {args.n_col} cols  (-x {args.n_row} -y {args.n_col})")

    # ── Step 1: clang → LLVM IR ──────────────────────────────────────────────
    print(f"\n[1/{n_steps}] Extracting LLVM IR with clang ...", flush=True)
    ll_file = os.path.join(satmapit_dir, "extracted.ll")
    run([clang, "-O3", "-fno-unroll-loops", "-fno-vectorize", "-fno-slp-vectorize",
         "-S", "-emit-llvm", "-o", ll_file, source_abs],
        cwd=satmapit_dir, desc="clang")

    # ── Step 2: opt CGRA extraction pass ─────────────────────────────────────
    print(f"[2/{n_steps}] Running CGRA extraction pass (opt) ...", flush=True)
    for d in find_acc_dirs(satmapit_dir):
        shutil.rmtree(os.path.join(satmapit_dir, d))
    run([opt, "-disable-output", ll_file, "-passes=cgra-extract"],
        cwd=satmapit_dir, desc="opt")
    os.remove(ll_file)

    acc_dirs = find_acc_dirs(satmapit_dir)
    if not acc_dirs:
        sys.exit("ERROR: opt pass produced no acc* directories — "
                 "check that the source file contains a #pragma cgra acc loop.")
    print(f"  Found {len(acc_dirs)} loop(s): {acc_dirs}")

    # ── Step 3: SAT-MapIt mapper ──────────────────────────────────────────────
    print(f"[3/{n_steps}] Running SAT-MapIt mapper (x={args.n_row}, y={args.n_col}) ...", flush=True)
    acc_code_files = []
    for acc_dir in acc_dirs:
        out_file = os.path.join(satmapit_dir, f"cgra-code-{acc_dir}")
        result = subprocess.run(
            [python, mapper, "-path", f"{acc_dir}/",
             "-x", str(args.n_row), "-y", str(args.n_col),
             "-r", "10", "-no_assembly", "1"],
            cwd=satmapit_dir, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  STDERR:\n{result.stderr}")
            sys.exit(f"ERROR: mapper failed for {acc_dir} (exit {result.returncode})")
        with open(out_file, "w") as fout:
            fout.write(result.stdout)
        acc_code_files.append(out_file)
        print(f"  Mapped {acc_dir} → {os.path.basename(out_file)}")
        shutil.rmtree(os.path.join(satmapit_dir, acc_dir))

    if len(acc_code_files) > 1:
        print(f"  WARNING: {len(acc_code_files)} loops found — only processing the first one.")
        print(f"           For the others, run satmapit_parse.py manually:")
        for f in acc_code_files[1:]:
            print(f"           python util/satmapit_parse.py {f} --name ...")

    # ── Step 4 (or final): satmapit_parse.py → instructions_*.py ─────────────
    here     = os.path.dirname(os.path.abspath(__file__))
    parse_py = os.path.join(here, "satmapit_parse.py")
    gen_py   = os.path.join(here, "cgra_gen.py")
    out_dir  = os.path.abspath(args.out_dir)
    acc_file = acc_code_files[0]

    if args.app:
        print(f"\n[4/{n_steps}] Generating instructions + app '{args.app}' ...", flush=True)
    else:
        print(f"\n[3/{n_steps}] done — generating instructions file ...", flush=True)

    result = subprocess.run(
        [sys.executable, parse_py, acc_file,
         "--name", name, "--n-row", str(args.n_row), "--n-col", str(args.n_col),
         "--out-dir", out_dir, "--pipeline"],
        capture_output=False
    )
    if result.returncode != 0:
        sys.exit("ERROR: satmapit_parse.py failed")

    draft = os.path.join(out_dir, name + ".py")

    if not args.app:
        print(f"\nNext: python util/cgra_gen.py {draft} <app_name>")
        return

    result2 = subprocess.run(
        [sys.executable, gen_py, draft, args.app,
         "--out-dir", out_dir, "--ref-src", source_abs],
        capture_output=False
    )
    if result2.returncode != 0:
        sys.exit("ERROR: cgra_gen.py failed")

    main_c = os.path.join(out_dir, args.app, "main.c")
    print(f"\n{'='*60}")
    print(f"Done: {main_c}")
    print(f"{'='*60}")
    print(f"""
Everything auto-generated — no edits needed:
  - CGRA bitstream (KMEM + CMEM)
  - Buffer sizes computed from schedule section breakdown
  - Stream pointer setup (read/write cols auto-detected)
  - Reference function {base}_ref() from your C source
  - Test data fill + PASS/FAIL verification

Simulate:  python run.py --simulator verilator --tests {args.app}
On FPGA:   python run.py --board zcu104 --tests {args.app}
""")


if __name__ == "__main__":
    main()
