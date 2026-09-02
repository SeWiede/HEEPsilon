#!/usr/bin/env python3
"""cgra_satmap.py — START HERE if you have a plain .c file with a
#pragma cgra acc loop. Full pipeline: C source → ready-to-run CGRA app in
one command.

(If you already have a cgra-code-acc1 from a manual SAT-MapIt run, use
util/satmapit_parse.py instead — skips the clang/opt/mapper steps below.
If you already have a reviewed instructions_*.py, use util/cgra_gen.py
directly to (re)generate main.c.)

Invokes clang to produce tagged LLVM IR, then hands it to util/cgra_tail.py
(opt → SAT-MapIt mapper → satmapit_parse.py → cgra_gen.py), which is shared
with the GLSL front end util/cgra_glsl.py.

Usage:
    python util/cgra_satmap.py <source.c> --app <app_name>

Example:
    python util/cgra_satmap.py benchmarks/vec_sum/vec_sum.c --app cgra_vec_sum
    python run.py --simulator verilator --tests cgra_vec_sum
    python run.py --board zcu104 --tests cgra_vec_sum

    # Without --app: stops after generating instructions_*.py only
    python util/cgra_satmap.py my_kernel.c

Output (with --app):
    sw/satmapit/<app>/main.c — a complete, runnable app. cgra_gen.py's own
    end-of-run summary (printed above the banner at the end of this run)
    lists anything it had to guess at that's worth reviewing before you
    trust it — read that, since it varies per kernel.

Everything is auto-generated:
    - CGRA bitstream (KMEM + CMEM arrays)
    - Buffer declarations with correct sizes (computed from section breakdown)
    - Stream pointer setup (which cols get read/write pointers, from CMEM scan)
    - Reference function (extracted from C source, pragma stripped, _ref suffix)
    - Benchmarking sweep + PASS/FAIL comparison to the reference function

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
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgra_tail as tail


_DEF_N_ROW, _DEF_N_COL = tail.grid_from_cgra_header()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",         help="C source file containing #pragma cgra acc loop")
    ap.add_argument("--name",         default=None,
                    help="Output name stem, e.g. 'instructions_vec_sum' "
                         "(default: instructions_<source basename>)")
    ap.add_argument("--n-row",        type=int, default=_DEF_N_ROW,
                    help=f"HEEPsilon N_ROW — SAT-MapIt -x value "
                         f"(default {_DEF_N_ROW}, from the generated cgra.h)")
    ap.add_argument("--n-col",        type=int, default=_DEF_N_COL,
                    help=f"HEEPsilon N_COL — SAT-MapIt -y value "
                         f"(default {_DEF_N_COL}, from the generated cgra.h)")
    ap.add_argument("--app",          default=None,
                    help="Generate sw/satmapit/<app>/main.c (complete, runnable)")
    ap.add_argument("--satmapit-dir", default=None,
                    help=f"Path to SAT-MapIt repo (default: {tail.DEFAULT_SATMAPIT_DIR})")
    # (unrecognised arguments are forwarded to cgra_gen.py — see cgra_tail.py)
    ap.add_argument("--out-dir",      default="sw/satmapit",
                    help="Output directory for instructions_*.py and app/ (default: sw/satmapit)")
    args, gen_extra = ap.parse_known_args()

    satmapit_dir = os.path.abspath(args.satmapit_dir or tail.DEFAULT_SATMAPIT_DIR)
    source_abs   = os.path.abspath(args.source)

    tail.check_satmapit(satmapit_dir, need_clang=True)
    if not os.path.exists(source_abs):
        sys.exit(f"ERROR: source file not found: {source_abs}")

    clang = os.path.join(satmapit_dir, tail.CLANG_REL)
    base  = os.path.splitext(os.path.basename(source_abs))[0]
    name  = args.name or f"instructions_{base}"
    steps = tail.Steps(4 if args.app else 3)

    print(f"=== cgra_satmap: {base} → {args.app or name} ===")
    print(f"SAT-MapIt dir : {satmapit_dir}")
    print(f"Source        : {source_abs}")
    print(f"CGRA size     : {args.n_row} rows × {args.n_col} cols  "
          f"(-x {args.n_row} -y {args.n_col})")

    # ── The front end: clang emits the loop tag from #pragma cgra acc ─────────
    # (clang/lib/CodeGen/CGLoopInfo.cpp:436 in the SAT-MapIt LLVM fork). The work
    # directory is the SAT-MapIt checkout, as it has always been: the extraction
    # pass writes acc*/ into its cwd and the mapper reads them from there.
    steps("Extracting LLVM IR with clang ...", blank_line=True)
    ll_file = os.path.join(satmapit_dir, "extracted.ll")
    tail.run([clang, "-O3", "-fno-unroll-loops", "-fno-vectorize", "-fno-slp-vectorize",
              "-S", "-emit-llvm", "-o", ll_file, source_abs],
             cwd=satmapit_dir, desc="clang")

    try:
        tail.drive_tail(ll_file,
                        work_dir=satmapit_dir, satmapit_dir=satmapit_dir,
                        name=name, n_row=args.n_row, n_col=args.n_col,
                        out_dir=os.path.abspath(args.out_dir), app=args.app,
                        ref_src=source_abs, gen_extra=gen_extra, steps=steps)
    finally:
        if os.path.exists(ll_file):
            os.remove(ll_file)


if __name__ == "__main__":
    main()
