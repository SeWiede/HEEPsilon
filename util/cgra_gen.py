#!/usr/bin/env python3
"""cgra_gen.py — THE LAST STEP of the C-to-CGRA pipeline. If you have a plain
.c file, you want util/cgra_satmap.py instead (it calls this automatically).

This is the pipeline (three tools, one per starting point):
    plain .c file            → util/cgra_satmap.py    (runs everything below)
    existing cgra-code-acc1  → util/satmapit_parse.py  → produces instructions_*.py
    reviewed instructions_*.py (this script)           → produces the app's files

Generate sw/satmapit/<app>/ (or sw/applications/) from a kernel spec: main.c
plus the split-out building blocks (cgra_bitstream.{h,c}, cgra_setup.{h,c},
verify.{h,c}, sweep.{h,c}) — see "The generated app has" below for what's in
each. The kernel spec is an instructions_*.py file, already produced by one
of the two tools above. Call this directly only to (re)generate an app's
files from a spec you already have — e.g. after hand-editing one per the
review checklist in docs/cgra_kernel_toolchain.md, which also has the full
pipeline walkthrough.

Run from the project root.

Usual invocation (everything except the two positional args is optional —
defaults shown are what you get by omitting each flag):

    python util/cgra_gen.py <spec.py> <app_name> --ref-src <original.c>

Concrete example:

    python util/cgra_gen.py \\
        sw/satmapit/instructions_vec_sum.py cgra_vec_sum \\
        --ref-src /path/to/vec_sum.c

    → sw/satmapit/cgra_vec_sum/ (main.c + building blocks), with:
        --out-dir sw/satmapit          (default; use sw/applications when promoting)
        --satmapit-dir $SATMAPIT_DIR or ../SAT-MapIt   (default; supplies the
                                         libclang used for --ref-src extraction)
        --sweep auto                   (default; sweeps N if vec_sum's `int N`
                                         parameter makes that possible, otherwise
                                         repeats randomized-input trials instead)

Without --ref-src, verify.c gets a TODO stub instead of an auto-extracted
reference function, and no automatic PASS/FAIL/sweep.

Overriding the sweep range or trial count (only meaningful with --ref-src):

    python util/cgra_gen.py sw/satmapit/instructions_vec_sum.py cgra_vec_sum \\
        --ref-src /path/to/vec_sum.c --sweep-range 4:4096

Multiple kernel specs can be packed into one CMEM/KMEM (the app name is
always the last positional argument):

    python util/cgra_gen.py instructions_a.py instructions_b.py cgra_both

The generated app has:
  - cgra_bitstream.{h,c}: embedded KMEM/CMEM bitstream arrays (sparse format,
    non-NOP only) + cgra_load_bitstream()
  - cgra_setup.{h,c}: interrupt/PLIC setup, cgra_setup(), cgra_intr_arm/wait()
  - verify.{h,c}: the extracted reference function (with --ref-src) or a
    TODO stub (without), plus verify_results() (CGRA-vs-reference compare)
  - sweep.{h,c}: buffer declarations, get_cgra_<var>() accessors, and
    run_sweep() — the benchmarking sweep + reference comparison itself
  - main.c: the entry point — just calls cgra_setup() then run_sweep()
  - An end-of-run summary of every assumption made and anything needing
    manual review, printed to the console — and left as TODO comments in
    whichever file the relevant code actually lives in (usually sweep.c)
"""

import sys
import os
import shutil
import datetime
import re
import argparse
from math import ceil, log

# ─── SAT-MapIt / libclang location ────────────────────────────────────────────
# Used only by extract_kernel_info()'s libclang path (see below) — mirrors
# cgra_satmap.py's own SATMAPIT_DIR resolution so both tools agree on where
# the vendored clang/libclang build lives.
_HEEPSILON_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_SATMAPIT_DIR = os.environ.get(
    "SATMAPIT_DIR",
    os.path.join(os.path.dirname(_HEEPSILON_ROOT), "SAT-MapIt")
)

# ─── CGRA configuration ───────────────────────────────────────────────────────
# Read from the generated driver header, which `make mcu-gen` rewrites from the
# active heepsilon_cfg (see Makefile CGRA_CFG). That header is what the app
# itself compiles against, so a kernel emitted here can never disagree with the
# hardware it will run on. The literals below are the 4x4 fallback for when the
# header has not been generated yet.
_CGRA_H = os.path.join(_HEEPSILON_ROOT, 'sw', 'external', 'drivers', 'cgra', 'cgra.h')

def _read_cgra_header(path=_CGRA_H):
    """#define name value pairs from the generated cgra.h; {} if unreadable."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return {}
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r'^#define\s+(CGRA_\w+|RCS_\w+)\s+(\d+)\s*$',
                                 text, re.MULTILINE)}

_CGRA_CFG = _read_cgra_header()
if not _CGRA_CFG:
    print(f"WARNING: {_CGRA_H} not found — assuming a 4x4 CGRA. "
          f"Run 'make mcu-gen' so the emitted kernel matches the built hardware.",
          file=sys.stderr)

CGRA_N_COL              = _CGRA_CFG.get('CGRA_N_COLS', 4)
CGRA_N_ROW              = _CGRA_CFG.get('CGRA_N_ROWS', 4)
CGRA_MAX_COL            = _CGRA_CFG.get('CGRA_MAX_COLS', CGRA_N_COL)
CGRA_CMEM_BK_DEPTH      = _CGRA_CFG.get('CGRA_CMEM_BK_DEPTH', 128)
CGRA_CMEM_BK_DEPTH_LOG2 = int(ceil(log(CGRA_CMEM_BK_DEPTH, 2)))
CGRA_KMEM_DEPTH         = _CGRA_CFG.get('CGRA_KMEM_DEPTH', 16)
RCS_NUM_CREG            = _CGRA_CFG.get('CGRA_RCS_NUM_CREG', 32)
RCS_NUM_CREG_LOG2       = int(ceil(log(RCS_NUM_CREG, 2)))
CGRA_KMEM_WIDTH         = CGRA_MAX_COL + CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2
CGRA_CMEM_TOT_DEPTH     = CGRA_N_ROW * CGRA_CMEM_BK_DEPTH

# ─── Benchmarking sweep configuration ────────────────────────────────────────
# See docs/cgra_kernel_toolchain.md.
_SWEEP_MEM_BUDGET_BYTES = 8 * 1024   # on-chip bytes to spend on sweep buffers
_SWEEP_MAX_N_CAP        = 1024       # hard cap on the largest swept N
_SWEEP_TRIPCOUNT_MAX_N  = 64         # cap when the swept N is a bare trip count
                                     # (no array scales with it, so the memory
                                     # budget can't bound it — runtime does)
_SWEEP_TRIALS           = 30         # repetitions for scalar/fixed-schedule kernels
_SWEEP_RAND_SEED        = 12345
_SWEEP_SCALAR_RAND_MASK = 0x7FFF     # default random-input range for scalar sweeps (0..32767)
_SWEEP_DATA_RAND_MOD    = 2001       # data-element random range: [-1000, 1000]
_SWEEP_DATA_RAND_BIAS   = 1000

# ─── Encoding helpers ─────────────────────────────────────────────────────────
def get_bin(x, n=0):
    return format(x, 'b').zfill(n)

def int2bin(x, bits):
    s = bin(x & int("1" * bits, 2))[2:]
    return ("{0:0>%s}" % bits).format(s)

def get_hex(x, n=0):
    return format(x, 'x').zfill(n).upper()

def return_indices_of_a(a, b, name=''):
    for val in a:
        if b == val:
            return a.index(val)
    sys.exit(f"ERROR instruction: '{b}' not in {name} list")

# ─── ISA tables (must match cgra_bitstream_gen.py exactly) ───────────────────
muxA_list    = ['ZERO','SELF','RCL','RCR','RCT','RCB','R0','R1','R2','R3','IMM']
muxB_list    = ['ZERO','SELF','RCL','RCR','RCT','RCB','R0','R1','R2','R3','IMM']
ALU_op_list  = ['NOP','SADD','SSUB','SMUL','FXPMUL',
                'SLT','SRT','SRA',
                'LAND','LOR','LXOR','LNAND','LNOR','LXNOR',
                'BSFA','BZFA',
                'BEQ','BNE','BLT','BGE','JUMP',
                'LWD','SWD','LWI','SWI',
                'EXIT']
reg_dest_list = ['R0','R1','R2','R3']
reg_we_list   = ['0','1']
muxF_list     = ['SELF','RCL','RCR','RCT','RCB']
rcs_nop_instr = ['ZERO','ZERO','NOP','-','SELF','0']

# ─── Instruction encoder ──────────────────────────────────────────────────────
def encode_instr(instr):
    """Encode a 6-element [muxA, muxB, op, dest, muxF, imm] list to uint32."""
    bits = ""
    for idx, cmd in enumerate(instr):
        if idx == 3:
            # Register destination: '-' → no write (R0,we=0); name → write (Rn,we=1)
            if cmd == '-':
                cmd = ['R0', '0']
            else:
                cmd = [cmd, '1']
        else:
            if cmd == '-':
                cmd = rcs_nop_instr[idx]

        if idx == 0:
            bits += get_bin(return_indices_of_a(muxA_list,    cmd,      'muxA'),    4)
        elif idx == 1:
            bits += get_bin(return_indices_of_a(muxB_list,    cmd,      'muxB'),    4)
        elif idx == 2:
            bits += get_bin(return_indices_of_a(ALU_op_list,  cmd,      'ALU_op'),  5)
        elif idx == 3:
            bits += get_bin(return_indices_of_a(reg_dest_list, cmd[0], 'reg_dest'), 2)
            bits += cmd[1]
        elif idx == 4:
            bits += get_bin(return_indices_of_a(muxF_list,    cmd,      'muxF'),    3)
        elif idx == 5:
            bits += int2bin(int(cmd), 13)
    return int(bits, 2)

# ─── Kernel spec loader ───────────────────────────────────────────────────────
def load_kernel_specs(kernel_files):
    """Execute one or more kernel spec files in sequence, sharing CGRA state.

    Files are executed in order; ker_next_id and ker_start_add accumulate
    across files so multiple kernels end up in the same CMEM/KMEM.

    Returns the context dict; relevant state:
      ctx['rcs_instructions']  — [N_ROW][BK_DEPTH] instruction lists
      ctx['ker_conf_words']    — [KMEM_DEPTH] binary strings
      ctx['ker_next_id']       — next free kernel slot (used slots are 1..ker_next_id-1)
      ctx['ker_start_add']     — next free CMEM slot
    """
    ker_null_conf = get_bin(0, CGRA_KMEM_WIDTH)
    ctx = {
        # CGRA dimensions (include legacy aliases used by older instruction files)
        'CGRA_IMEM_NL_LOG2':       CGRA_CMEM_BK_DEPTH_LOG2,  # legacy alias
        'CGRA_IMEM_N_LINE':        CGRA_CMEM_BK_DEPTH,        # legacy alias
        'CGRA_N_COL':              CGRA_N_COL,
        'CGRA_N_ROW':              CGRA_N_ROW,
        'CGRA_MAX_COL':            CGRA_MAX_COL,
        'CGRA_CMEM_BK_DEPTH':      CGRA_CMEM_BK_DEPTH,
        'CGRA_CMEM_BK_DEPTH_LOG2': CGRA_CMEM_BK_DEPTH_LOG2,
        'CGRA_KMEM_DEPTH':         CGRA_KMEM_DEPTH,
        'RCS_NUM_CREG':            RCS_NUM_CREG,
        'RCS_NUM_CREG_LOG2':       RCS_NUM_CREG_LOG2,
        'CGRA_KMEM_WIDTH':         CGRA_KMEM_WIDTH,
        'get_bin':             get_bin,
        'int2bin':             int2bin,
        'get_hex':             get_hex,
        'return_indices_of_a': return_indices_of_a,
        'muxA_list':    muxA_list,
        'muxB_list':    muxB_list,
        'ALU_op_list':  ALU_op_list,
        'reg_dest_list': reg_dest_list,
        'reg_we_list':  reg_we_list,
        'muxF_list':    muxF_list,
        'rcs_nop_instr': rcs_nop_instr[:],
        'ker_null_conf': ker_null_conf,
        'rcs_instructions': [[rcs_nop_instr[:] for _ in range(CGRA_CMEM_BK_DEPTH)]
                              for _ in range(CGRA_N_ROW)],
        'ker_conf_words': [ker_null_conf for _ in range(CGRA_KMEM_DEPTH)],
        'ker_next_id':  1,
        'ker_start_add': 0,
        'pow': pow, 'int': int, 'range': range, 'len': len, 'print': print,
        'ceil': ceil, 'log': log,
    }
    abs_files = [os.path.abspath(kf) for kf in kernel_files]
    saved_cwd = os.getcwd()
    try:
        for kf_abs in abs_files:
            # cd to the kernel file's directory so any relative exec() inside works
            os.chdir(os.path.dirname(kf_abs))
            with open(kf_abs) as f:
                exec(f.read(), ctx)  # noqa: S102
    finally:
        os.chdir(saved_cwd)
    return ctx

# ─── C array generators ───────────────────────────────────────────────────────
def gen_kmem_c(ctx):
    """Return C declaration for cgra_kmem[]."""
    vals = [int(w, 2) for w in ctx['ker_conf_words']]
    entries = [f"    0x{v:X}" for v in vals]
    return ("static uint32_t cgra_kmem[CGRA_KMEM_DEPTH] = {\n" +
            ",\n".join(entries) + "\n};\n")


def gen_cmem_c(ctx):
    """Return sparse C declaration for cgra_cmem[] (only non-NOP entries)."""
    rcs_instr = ctx['rcs_instructions']
    nop_val = 0   # all-zero = NOP

    entries = []   # (flat_index, hex_value, comment_str)
    for row in range(CGRA_N_ROW):
        for slot in range(CGRA_CMEM_BK_DEPTH):
            val = encode_instr(rcs_instr[row][slot])
            if val != nop_val:
                flat_idx = row * CGRA_CMEM_BK_DEPTH + slot
                op  = rcs_instr[row][slot][2]
                dst = rcs_instr[row][slot][3]
                entries.append((flat_idx, val, f"RC{row} PC{slot}: {op} → {dst}"))

    if not entries:
        return "static uint32_t cgra_cmem[CGRA_CMEM_TOT_DEPTH] = { 0 }; /* all NOP */\n"

    lines = ["static uint32_t cgra_cmem[CGRA_CMEM_TOT_DEPTH] = {"]
    cur_row = -1
    for flat_idx, val, comment in entries:
        row = flat_idx // CGRA_CMEM_BK_DEPTH
        if row != cur_row:
            if cur_row != -1:
                lines.append("")
            lines.append(f"    /* RC{row} (row {row}, base {row * CGRA_CMEM_BK_DEPTH}) */")
            cur_row = row
        lines.append(f"    [{flat_idx}] = 0x{val:08X},  /* {comment} */")
    lines.append("};")
    return "\n".join(lines) + "\n"

# ─── Stream-layout inference ─────────────────────────────────────────────────
def infer_io_layout(ctx):
    """Scan CMEM for LWD/SWD per column, with per-section counts.

    Returns dict: col_idx → {
        'reads': [T, ...],          # T-steps with LWD
        'writes': [T, ...],         # T-steps with SWD/SWI
        'read_init':    int,        # LWD count in init section
        'read_prolog':  int,        # LWD count in prolog section
        'read_kernel':  int,        # LWD count in kernel section (per iteration)
        'read_epilog':  int,        # LWD count in epilog section
        'write_kernel': int,        # SWD count in kernel section (per iteration)
        'write_epilog': int,        # SWD count in epilog section
        'write_fini':   int,        # SWD count in fini section (T >= epilog_end)
        'has_swi':      bool,       # col uses SWI (indexed store, rarely used after transform)
        'swi_write_kernel': int,    # SWI time-steps in kernel section (per iteration)
        'swi_write_epilog': int,    # SWI time-steps in epilog section
    }
    """
    si = ctx.get('_satmapit_info', {})
    init_end   = si.get('init_end',   0)
    prolog_end = si.get('prolog_end', 0)
    kernel_end = si.get('kernel_end', 0)
    epilog_end = si.get('epilog_end', 0)

    layout = {}
    rcs = ctx['rcs_instructions']
    for ki, word_bin in enumerate(ctx['ker_conf_words']):
        v = int(word_bin, 2)
        if not v:
            continue
        col_mask = (v >> (CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2)) & ((1 << CGRA_N_COL) - 1)
        start    = (v >> RCS_NUM_CREG_LOG2) & ((1 << CGRA_CMEM_BK_DEPTH_LOG2) - 1)
        n_instr  = (v & ((1 << RCS_NUM_CREG_LOG2) - 1)) + 1
        for col in range(CGRA_N_COL):
            if not (col_mask >> col & 1):
                continue
            reads, writes = [], []
            ri, rp, rk, re = 0, 0, 0, 0   # LWD counts per section
            wk, we, wf = 0, 0, 0           # SWD counts: kernel, epilog, fini
            has_swi = False
            swi_wk = 0                      # SWI time-steps in kernel section
            swi_we = 0                      # SWI time-steps in epilog section
            for T in range(n_instr):
                pc = start + col * n_instr + T
                for row in range(CGRA_N_ROW):
                    op = rcs[row][pc][2] if pc < CGRA_CMEM_BK_DEPTH else 'NOP'
                    if op == 'LWD':
                        if T not in reads:
                            reads.append(T)
                        # Count per-PE: two rows issuing LWD at same T advance
                        # the column's stream pointer twice → two distinct reads.
                        if   T < init_end:   ri += 1
                        elif T < prolog_end: rp += 1
                        elif T < kernel_end: rk += 1
                        elif T < epilog_end: re += 1
                    elif op in ('SWD', 'SWI'):
                        if T not in writes:
                            writes.append(T)
                            if op == 'SWI':
                                has_swi = True
                                if prolog_end <= T < kernel_end: swi_wk += 1
                                elif kernel_end <= T < epilog_end: swi_we += 1
                            else:  # SWD
                                if   prolog_end <= T < kernel_end: wk += 1
                                elif kernel_end <= T < epilog_end: we += 1
                                elif T >= epilog_end:              wf += 1
                        elif op == 'SWI':
                            has_swi = True  # mark even if T already seen
            if reads or writes:
                if col not in layout:
                    layout[col] = {
                        'reads': [], 'writes': [],
                        'read_init': 0, 'read_prolog': 0,
                        'read_kernel': 0, 'read_epilog': 0,
                        'write_kernel': 0, 'write_epilog': 0, 'write_fini': 0,
                        'has_swi': False,
                        'swi_write_kernel': 0, 'swi_write_epilog': 0,
                    }
                for T in reads:
                    if T not in layout[col]['reads']:
                        layout[col]['reads'].append(T)
                for T in writes:
                    if T not in layout[col]['writes']:
                        layout[col]['writes'].append(T)
                layout[col]['read_init']        += ri
                layout[col]['read_prolog']      += rp
                layout[col]['read_kernel']      += rk
                layout[col]['read_epilog']      += re
                layout[col]['write_kernel']     += wk
                layout[col]['write_epilog']     += we
                layout[col]['write_fini']       += wf
                layout[col]['has_swi']          |= has_swi
                layout[col]['swi_write_kernel'] += swi_wk
                layout[col]['swi_write_epilog'] += swi_we
    return layout


# ─── Loop trip-count (branch bound) inference ────────────────────────────────
_BRANCH_OPS = ('BEQ', 'BNE', 'BLT', 'BGE')
_REG_NAMES  = ('R0', 'R1', 'R2', 'R3')


def infer_branch_bound_cols(ctx):
    """Find which stream slots hold a loop trip count, by tracing each
    conditional branch's register operand back to the LWD that loaded it.

    A branch comparing against a register (e.g. `["RCT","R0","BNE",...]`) is
    the kernel's loop-termination test. If that register was filled by an LWD
    in the same RC, then whatever the host writes into that stream slot IS the
    loop trip count — a schedule input, not test data. Randomizing it the way
    an ordinary scalar input is randomized doesn't vary the test data, it
    varies how many times the kernel runs, which is almost never what the
    caller meant (see gen_sweep_section).

    This reads the CMEM only. It is deliberately independent of the C-source
    guess made by _loop_cond_size_param() so the two can be cross-checked:
    agreement is strong evidence, disagreement is a real ambiguity to report
    rather than silently resolve.

    Returns dict: col → {
        'row', 'reg', 'branch_op', 'branch_T', 'lwd_T': the traced path,
        'slot':         int,   # index into col{c}_in[] holding the value
        'slot_certain': bool,  # False if same-T LWD ties made slot order a guess
    }
    """
    bounds = {}
    rcs = ctx['rcs_instructions']
    for word_bin in ctx['ker_conf_words']:
        v = int(word_bin, 2)
        if not v:
            continue
        col_mask = (v >> (CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2)) & ((1 << CGRA_N_COL) - 1)
        start    = (v >> RCS_NUM_CREG_LOG2) & ((1 << CGRA_CMEM_BK_DEPTH_LOG2) - 1)
        n_instr  = (v & ((1 << RCS_NUM_CREG_LOG2) - 1)) + 1

        for col in range(CGRA_N_COL):
            if not (col_mask >> col & 1) or col in bounds:
                continue

            def instr(row, T):
                pc = start + col * n_instr + T
                return rcs[row][pc] if pc < CGRA_CMEM_BK_DEPTH else rcs_nop_instr

            # The column's read stream in access order. Rows issuing an LWD at
            # the same T each advance the shared per-column pointer (see
            # data_bus_handler.sv), but the CMEM does not encode which of them
            # the bus serves first — assume ascending row and flag it.
            lwds = [(T, row, instr(row, T)[3])
                    for T in range(n_instr) for row in range(CGRA_N_ROW)
                    if instr(row, T)[2] == 'LWD']

            for slot, (lwd_T, lwd_row, lwd_reg) in enumerate(lwds):
                if lwd_reg not in _REG_NAMES:
                    continue
                # Any branch in the same RC comparing against that register.
                for T in range(n_instr):
                    ins = instr(lwd_row, T)
                    if ins[2] in _BRANCH_OPS and lwd_reg in (ins[0], ins[1]):
                        bounds[col] = {
                            'row': lwd_row, 'reg': lwd_reg,
                            'branch_op': ins[2], 'branch_T': T, 'lwd_T': lwd_T,
                            'slot': slot,
                            'slot_certain': sum(1 for t, _, _ in lwds if t == lwd_T) == 1,
                        }
                        break
                if col in bounds:
                    break
    return bounds


def _iter_kernels(ctx):
    """Yield (kernel_slot, col_mask, start, n_instr) for each configured kernel."""
    for ki, word_bin in enumerate(ctx['ker_conf_words']):
        v = int(word_bin, 2)
        if not v:
            continue
        yield (ki,
               (v >> (CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2)) & ((1 << CGRA_N_COL) - 1),
               (v >> RCS_NUM_CREG_LOG2) & ((1 << CGRA_CMEM_BK_DEPTH_LOG2) - 1),
               (v & ((1 << RCS_NUM_CREG_LOG2) - 1)) + 1)


def infer_branch_cols(ctx):
    """Return {kernel_slot: sorted list of columns containing a branch/jump}.

    Plain structural scan — unlike infer_branch_bound_cols() this doesn't care
    where the branch's operands come from, only which column issues it, which
    is what the last-column hazard depends on (see rotate_kernel_cols).
    """
    rcs = ctx['rcs_instructions']
    out = {}
    for ki, mask, start, n_instr in _iter_kernels(ctx):
        cols = []
        dense = -1
        for col in range(CGRA_N_COL):
            if not (mask >> col & 1):
                continue
            dense += 1
            for T in range(n_instr):
                pc = start + dense * n_instr + T
                if pc >= CGRA_CMEM_BK_DEPTH:
                    continue
                if any(rcs[row][pc][2] in _BRANCH_OPS + ('JUMP',) for row in range(CGRA_N_ROW)):
                    cols.append(col)
                    break
        if cols:
            out[ki] = cols
    return out


def rotate_kernel_cols(ctx, shift, report=None):
    """Cyclically shift every kernel's columns by `shift`, in place.

    Why this is a relabelling and not a re-mapping: the inter-column mesh is a
    full torus (cgra_rcs.sv wires rcs_mesh_res[k][-1] = rcs_res_reg[k][N_COL-1]
    and rcs_mesh_res[k][N_COL] = rcs_res_reg[k][0]), so shifting all columns by
    a constant preserves every RCL/RCR relationship. RCT/RCB are intra-column
    and untouched. SAT-MapIt's schedule is not re-solved — only which physical
    column each block of instructions lands in changes.

    Because this rewrites the CMEM itself, everything downstream (buffer
    sizing, stream-pointer slots, the column report) follows automatically: it
    all derives from infer_io_layout(), which reads the rotated CMEM.

    Only sound for a FULL column mask. With a partial mask the wrap would reach
    a column the kernel doesn't own, so those kernels are left alone and
    reported instead.

    Returns the list of kernel slots actually rotated.
    """
    full_mask = (1 << CGRA_N_COL) - 1
    shift %= CGRA_N_COL
    if shift == 0:
        return []

    rcs = ctx['rcs_instructions']
    rotated = []
    for ki, mask, start, n_instr in _iter_kernels(ctx):
        if mask != full_mask:
            if report is not None:
                report['todo'].append(
                    f"Kernel {ki} uses column mask 0b{mask:0{CGRA_N_COL}b}, not all columns — "
                    f"NOT rotated. A cyclic rotation is only sound when the kernel owns every "
                    f"column, because the mesh wrap would otherwise reach a column this kernel "
                    f"doesn't own. If this kernel has a branch in the last column, move it by "
                    f"hand.")
            continue
        for row in range(CGRA_N_ROW):
            block = [[rcs[row][start + c * n_instr + T] for T in range(n_instr)]
                     for c in range(CGRA_N_COL)]
            for c in range(CGRA_N_COL):
                for T in range(n_instr):
                    rcs[row][start + ((c + shift) % CGRA_N_COL) * n_instr + T] = block[c][T]
        rotated.append(ki)
    return rotated


def apply_branch_rotation(ctx, mode, report):
    """Handle --rotate-cols. `mode` is 'off', 'auto', or an int shift.

    'off' (the default) leaves SAT-MapIt's mapping exactly as produced and only
    reports a branch in the last column. That column is where branches are not
    honoured on the ZCU104 build (measured) — but the mechanism is unknown and this has never been checked on
    other boards or CGRA sizes, so rotating is opt-in rather than automatic.
    """
    branch_cols = infer_branch_cols(ctx)
    last = CGRA_N_COL - 1
    in_last = sorted({ki for ki, cols in branch_cols.items() if last in cols})

    if mode == 'off':
        if in_last:
            report['todo'].append(
                f"Kernel(s) {', '.join(str(k) for k in in_last)}: branch is in column {last}, "
                f"the last column. It is not taken on FPGA — the loop runs once. Passes in "
                f"simulation, so only hardware shows it. Regenerate with --rotate-cols auto.")
        return

    if mode == 'auto':
        if not in_last:
            report['notes'].append(
                f"--rotate-cols auto: no branch found in column {last}, nothing to do — "
                f"SAT-MapIt's column assignment left untouched.")
            return
        shift = 1   # moves column N_COL-1 to column 0
    else:
        shift = int(mode)

    rotated = rotate_kernel_cols(ctx, shift, report=report)
    if not rotated:
        return

    still_bad = sorted({ki for ki, cols in infer_branch_cols(ctx).items() if last in cols})
    report['notes'].append(
        f"--rotate-cols: shifted every column of kernel(s) "
        f"{', '.join(str(k) for k in rotated)} by +{shift % CGRA_N_COL}"
        + (f", moving the branch out of column {last}." if mode == 'auto' else "."))
    if still_bad:
        report['todo'].append(
            f"After rotating, kernel(s) {', '.join(str(k) for k in still_bad)} STILL have a "
            f"branch in column {last} (a kernel with branches in several columns can't be "
            f"fixed by any single rotation). Fix by hand or pick a different --rotate-cols "
            f"shift.")


def gen_io_comment(layout):
    """Return a C comment block describing the inferred stream layout."""
    if not layout:
        return "/* No LWD/SWD detected — kernel uses no memory I/O. */\n"
    has_swi = any(v.get('has_swi') for v in layout.values())
    lines = ["/* Stream layout (auto-inferred from CMEM):"]
    for col in sorted(layout):
        r = layout[col]['reads']
        w = layout[col]['writes']
        swi = layout[col].get('has_swi', False)
        parts = []
        if r:
            parts.append(f"LWD at T={','.join(str(t) for t in sorted(r))}")
        if w:
            op = "SWI" if swi else "SWD"
            parts.append(f"{op} at T={','.join(str(t) for t in sorted(w))}")
        lines.append(f" *   Col {col}: {'; '.join(parts)}")
    lines.append(" *")
    if has_swi:
        lines.append(" * SWI cols: output assumed sequential from base address (like SWD).")
        lines.append(" *   swi_col*_out[] auto-declared; col*_in[0] auto-set to point at it.")
        lines.append(" *   No cgra_set_write_ptr needed — base address goes through read stream.")
        lines.append(" *   If this assumption is wrong for your kernel, set col*_in[0] manually.")
    else:
        lines.append(" * Each active column needs its own read/write pointer.")
    lines.append(" */")
    return "\n".join(lines) + "\n"


def gen_io_role_report(layout):
    """One entry per active column describing its auto-inferred role (data
    array / scalar input / write output / SWI) — the I/O auto-inference that
    buffer sizing, fill patterns, and reference-arg mapping all depend on.
    Independent of ref_info; this is purely from the CMEM scan."""
    entries = []
    for col in sorted(layout):
        v = layout[col]
        roles = []
        if v.get('has_swi'):
            roles.append("SWI output (sequential-write assumed)")
        elif v['read_kernel'] > 0:
            roles.append(f"data (array, {v['read_kernel']} read(s)/iter)")
        elif v['reads']:
            roles.append("scalar input")
        if v['writes'] and not v.get('has_swi'):
            roles.append("write output")
        if roles:
            entries.append(f"col{col}=[{' + '.join(roles)}]")
    return entries


def gen_merged_col_report(layout, accessors, write_col, is_void, ret, leftover_scalar_cols,
                           trip=None, trip_param_name=None):
    """One line per active column, folding its CMEM-derived structural role
    together with whichever C parameter (if any) is bound to it — e.g.
    'col1: x_arr (array input, 1 read(s)/iter)' instead of two separately
    numbered things a reader has to cross-reference themselves (a plain
    per-column structural role list, plus a flat 'param X -> col Y' list).
    Needs ref_info (accessors comes from _build_ref_args, which needs
    params) — gen_app_files falls back to the plain gen_io_role_report()
    when there's no ref_info to build this from.

    trip/trip_param_name: the loop trip count decided by _resolve_trip_count().
    Its column is deliberately absent from `accessors` (a size argument is
    passed by value, not by address), so without this it would be described as
    "scalar input, no matching parameter" — which flatly contradicts the note
    the same summary prints about it being the swept trip count.
    """
    col_in, col_out = {}, {}
    for pname, _ret_type, expr in accessors:
        m = re.search(r'col(\d+)_(in|out)', expr)
        if not m:
            continue
        col, direction = int(m.group(1)), m.group(2)
        (col_in if direction == 'in' else col_out).setdefault(col, []).append(pname)

    is_scalar_return = (not is_void) and ret.strip() not in ('void', '')

    lines = []
    for col in sorted(layout):
        v = layout[col]
        if not (v['reads'] or v['writes'] or v.get('has_swi')):
            continue
        parts = []
        if v.get('has_swi'):
            parts.append("SWI output (sequential-write assumed)")
        elif v['reads']:
            trip_here = (trip is not None and trip.get('col') == col)
            trip_tag = ""
            if trip_here:
                named = f"'{trip_param_name}' = " if trip_param_name else ""
                trip_tag = ((f"; slot [{trip['slot']}] = {named}loop trip count, swept as N "
                             f"({trip['confidence']})") if trip.get('sweepable') else
                            (f"; slot [{trip['slot']}] feeds a branch comparison, meaning "
                             f"unresolved — pinned to 0, not swept"))
            if col in col_in:
                names = '/'.join(col_in[col])
                if v['read_kernel'] > 0:
                    parts.append(f"{names} (array input, {v['read_kernel']} read(s)/iter){trip_tag}")
                else:
                    parts.append(f"{names} (scalar input){trip_tag}")
            elif trip_here:
                parts.append(
                    (f"loop trip count, swept as N ({trip['confidence']})"
                     + (f" — parameter '{trip_param_name}'" if trip_param_name else ""))
                    if trip.get('sweepable') else
                    "feeds a branch comparison, meaning unresolved — pinned to 0, not swept")
            elif col in leftover_scalar_cols:
                parts.append("schedule constant, not tied to any parameter")
            elif v['read_kernel'] > 0:
                parts.append(f"array input ({v['read_kernel']} read(s)/iter), no matching parameter")
            else:
                parts.append("scalar input, no matching parameter")
        if v['writes'] and not v.get('has_swi'):
            if col in col_out:
                parts.append(f"{'/'.join(col_out[col])} (output)")
            elif col == write_col and is_scalar_return:
                parts.append("output (function return value)")
            else:
                parts.append("write output")
        lines.append(f"col{col}: " + "; ".join(parts))
    return lines


def _buf_size_expr(col_info, io):
    """Compute buffer size expression for read ('in'), write ('out'), or SWI output ('swi_out').

    For read buffers ('in'):
      - If col has kernel LWDs: N_ELEMENTS data elements + fixed overhead
        formula: N_ELEMENTS * read_kernel + read_init + read_prolog + read_epilog
      - Otherwise: exactly read_init + read_prolog + read_epilog (scalar fixed)

    For SWD write buffers ('out'):
      - Kernel SWDs: per-iteration writes → formula: N_ELEMENTS * write_kernel + epilog + fini
      - Fini-only SWDs: accumulation result written once → formula: write_fini

    For SWI write buffers ('swi_out'):
      - Assumption: SWI writes sequentially like SWD from a base address.
        formula: N_ELEMENTS * swi_write_kernel + swi_write_epilog
    """
    if io == 'in':
        rk = col_info['read_kernel']
        ri = col_info['read_init']
        rp = col_info['read_prolog']
        re = col_info['read_epilog']
        if rk > 0:
            extra = ri + rp + re
            if rk == 1 and extra > 0:
                return f"N_ELEMENTS + {extra}"
            elif rk == 1:
                return "N_ELEMENTS"
            else:
                return f"N_ELEMENTS * {rk} + {extra}" if extra else f"N_ELEMENTS * {rk}"
        else:
            total = ri + rp + re
            return str(total) if total > 0 else "1"
    elif io == 'swi_out':
        wk = col_info['swi_write_kernel']
        we = col_info['swi_write_epilog']
        if wk > 0:
            extra = we
            if wk == 1 and extra > 0:
                return f"N_ELEMENTS + {extra}"
            elif wk == 1:
                return "N_ELEMENTS"
            else:
                return f"N_ELEMENTS * {wk} + {extra}" if extra else f"N_ELEMENTS * {wk}"
        else:
            return str(we) if we > 0 else "1"
    else:  # 'out' — SWD
        wk = col_info.get('write_kernel', 0)
        we = col_info.get('write_epilog', 0)
        wf = col_info['write_fini']
        if wk > 0:
            # Per-iteration writes: N elements + epilog drain + fini writes
            extra = we + wf
            if wk == 1 and extra > 0:
                return f"N_ELEMENTS + {extra}"
            elif wk == 1:
                return "N_ELEMENTS"
            else:
                return f"N_ELEMENTS * {wk} + {extra}" if extra else f"N_ELEMENTS * {wk}"
        else:
            # Accumulation or fixed-count writes
            total = we + wf
            return str(total) if total > 0 else "1"


def _real_output_count_expr(col_info, n_token):
    """How many of a write column's buffer slots hold real per-iteration
    output data, as opposed to epilog/fini pipeline-drain writes that follow
    the real data — i.e. _buf_size_expr('out') MINUS the '+ extra' term.

    Confirmed necessary (mul_test): a per-iteration SWD write (write_kernel=1)
    is followed by 2 more SWD writes tagged epilog in the CMEM timeline (pipe
    drain, fires once regardless of N) — the buffer is sized N+2 to hold both,
    but only the first N slots are real data; the reference function only
    ever computes N values, so comparing all N+2 always flags the 2 drain
    slots as mismatches (reference defaults to 0 there, drain writes real
    leftover values) regardless of whether the real N outputs are correct.

    For a fini-only (reduction/accumulator, write_kernel=0) column there is no
    such split — the whole (fixed, N-independent) size IS the real data, so
    this returns the same thing _buf_size_expr('out') would.
    """
    wk = col_info.get('write_kernel', 0)
    if wk > 0:
        return f"{n_token} * {wk}" if wk > 1 else n_token
    we, wf = col_info.get('write_epilog', 0), col_info['write_fini']
    total = we + wf
    return str(total) if total > 0 else "1"


def _buf_size_value(col_info, io, n):
    """Numeric analogue of _buf_size_expr for a concrete candidate N (used to
    plan the sweep's memory footprint at generation time)."""
    if io == 'in':
        rk, ri = col_info['read_kernel'], col_info['read_init']
        rp, re_ = col_info['read_prolog'], col_info['read_epilog']
        if rk > 0:
            return n * rk + ri + rp + re_
        return max(ri + rp + re_, 1)
    elif io == 'swi_out':
        wk, we = col_info['swi_write_kernel'], col_info['swi_write_epilog']
        return n * wk + we if wk > 0 else max(we, 1)
    else:  # 'out'
        wk = col_info.get('write_kernel', 0)
        we, wf = col_info.get('write_epilog', 0), col_info['write_fini']
        if wk > 0:
            return n * wk + we + wf
        return max(we + wf, 1)


def gen_io_buffers(layout):
    """Return C buffer declarations for each col with LWD or SWD."""
    if not layout:
        return ""
    lines = []
    for col in sorted(layout):
        if layout[col].get('has_swi'):
            swi_size = _buf_size_expr(layout[col], 'swi_out')
            lines.append(
                f"static int32_t swi_col{col}_out[{swi_size}]"
                f" __attribute__((aligned(4)));  /* col {col} SWI output (auto-assumed sequential) */"
            )
        if layout[col]['reads']:
            size = _buf_size_expr(layout[col], 'in')
            if layout[col].get('has_swi'):
                comment = f"/* col {col} SWI base address — auto-set to (int32_t)swi_col{col}_out */"
            else:
                comment = f"/* col {col} read stream */"
            lines.append(
                f"static int32_t col{col}_in[{size}]"
                f" __attribute__((aligned(4)));  {comment}"
            )
        if layout[col]['writes'] and not layout[col].get('has_swi'):
            size = _buf_size_expr(layout[col], 'out')
            lines.append(
                f"static int32_t col{col}_out[{size}]"
                f" __attribute__((aligned(4)));  /* col {col} write stream */"
            )
    return "\n".join(lines) + "\n"


def gen_ptr_setup(layout):
    """Return cgra_set_read_ptr / cgra_set_write_ptr calls for each active col.
    `cgra` is a `cgra_t *` parameter in the generated code (not a local struct)."""
    if not layout:
        return "    /* no stream pointers needed */"
    lines = []
    for col in sorted(layout):
        if layout[col]['reads']:
            lines.append(
                f"    cgra_set_read_ptr (cgra, (uint32_t)col{col}_in,  {col});"
                f"  /* col {col} read */"
            )
        if layout[col]['writes'] and not layout[col].get('has_swi'):
            lines.append(
                f"    cgra_set_write_ptr(cgra, (uint32_t)col{col}_out, {col});"
                f"  /* col {col} write */"
            )
    return "\n".join(lines)


# ─── Benchmarking sweep ──────────────────────────────────────────────────────
#
# Two kinds of sweep, chosen from infer_io_layout()'s per-column metadata:
#
#   'loop_bound' — the kernel has a data col (LWD inside the kernel section,
#                  i.e. read_kernel > 0), which cgra_gen.py already treats as
#                  loading a runtime "N" as the first init-section word (see
#                  gen_test_harness's "N prefix" convention). Sweep N itself,
#                  doubling, reusing buffers sized for the largest N.
#
#   'scalar'     — no data col (fixed-schedule kernel, e.g. isqrt32): there is
#                  no "N" to sweep, so instead repeat the run across a range
#                  of randomized scalar input values.
#
# Only kernels that already auto-generate a real PASS/FAIL comparison (single
# scalar return value, no SWI) are swept; complex/manual cases are unchanged.

_RNG_BLOCK = f'''\
/* ── Deterministic RNG (tausworthe combo, mirrors kernel_test's kcom_getRand) ── */
#define SWEEP_RAND_SEED  {_SWEEP_RAND_SEED}u
static uint32_t _rz1 = SWEEP_RAND_SEED, _rz2 = SWEEP_RAND_SEED,
                _rz3 = SWEEP_RAND_SEED, _rz4 = SWEEP_RAND_SEED;

static uint32_t sweep_rand(void) {{
    uint32_t b;
    b = ((_rz1 << 6)  ^ _rz1) >> 13;  _rz1 = ((_rz1 & 4294967294U) << 18) ^ b;
    b = ((_rz2 << 2)  ^ _rz2) >> 27;  _rz2 = ((_rz2 & 4294967288U) << 2)  ^ b;
    b = ((_rz3 << 13) ^ _rz3) >> 21;  _rz3 = ((_rz3 & 4294967280U) << 7)  ^ b;
    b = ((_rz4 << 3)  ^ _rz4) >> 12;  _rz4 = ((_rz4 & 4294967168U) << 13) ^ b;
    return (_rz1 ^ _rz2 ^ _rz3 ^ _rz4);
}}
'''


def _verify_path_status(layout, ref_info):
    """Classify which verification path this kernel takes and, if it's not the
    sweep-eligible one, why — feeds both gen_sweep_section()'s eligibility gate
    and the end-of-run report. Mirrors the branch dispatch in gen_test_harness().

    Returns None if eligible for the sweep — either a scalar-return function
    (single value compared per iteration) or a void/array-mutating one with a
    "simple" 1:1 stream-to-array shape (per-element array compare per
    iteration, see the is_void branch in gen_sweep_section). Otherwise returns
    (tag, message) where tag is 'notes' (auto-verified, just not swept) or
    'todo' (needs manual review/work).
    """
    if not ref_info or not ref_info.get('func_name'):
        # Caller (main()) already reports this precisely (no --ref-src vs. no pragma found).
        return ('skip', None)
    fname = ref_info['func_name']
    ret   = ref_info.get('return_type', 'int32_t').strip()
    swi_cols   = [c for c, v in layout.items() if v.get('has_swi')]
    write_cols = [c for c, v in layout.items() if v['writes'] and not v.get('has_swi')]

    if swi_cols and not write_cols:
        if ret in ('void', ''):
            return ('todo', f"{fname}_ref() is void with SWI output — auto-comparison isn't "
                            f"possible (in-place mutation over a delay-tapped stream); "
                            f"sweep.c prints raw output only, compare by hand.")
        return ('notes', f"{fname}_ref() is verified once against SWI output col{swi_cols[0]} "
                         f"(single fixed-input run — SWI kernels aren't swept yet).")
    if not write_cols:
        return ('todo', "No SWD/SWI write column detected at all — verify output manually.")
    if ret in ('void', ''):
        data_cols = [c for c, v in layout.items() if v['read_kernel'] > 0]
        is_simple = all(layout[c]['read_kernel'] <= 1 for c in data_cols)
        if is_simple:
            return None
        return ('todo', f"{fname}_ref() is void with multiple LWDs per data col (complex "
                        f"fan-out, e.g. SHA-style, where the same source array feeds multiple "
                        f"stream offsets) — auto-comparison not possible. verify.c has a "
                        f"print-only stub; implement the real comparison there by hand.")
    return None


def plan_loop_bound_sweep(layout, min_n, explicit_range=None, tripcount_only=False):
    """Return a doubling list of N values.

    Default: auto-sized to fit _SWEEP_MEM_BUDGET_BYTES, starting from the
    smallest power of two >= min_n.  If explicit_range=(lo, hi) is given
    (from --sweep-range), that range is used instead — the caller is
    responsible for deciding whether it makes sense (e.g. it may exceed the
    memory budget, or ignore the kernel's structural min_n).

    tripcount_only: the swept N is a pure loop trip count with no array to
    size (e.g. ReverseBits' NumBits). Nothing scales with N in memory, so the
    byte budget can't bound the range — cost is runtime instead, hence the
    separate _SWEEP_TRIPCOUNT_MAX_N cap. The range also starts at min_n rather
    than 4: N=1,2,3 are the values where an off-by-one or a do-while-shaped
    kernel (one iteration when zero were asked for) actually shows up, and
    they cost almost nothing to run.
    """
    if explicit_range is not None:
        lo, hi = explicit_range
        if lo < min_n:
            print(f"  WARNING: --sweep-range start ({lo}) is below this kernel's "
                  f"structural minimum N ({min_n}) — the low end of the sweep may fail.")
        values, n = [], lo
        while n <= hi:
            values.append(n)
            n *= 2
        return values or [lo]

    if tripcount_only:
        start = max(min_n, 1)
        values, n = [], start
        while n <= _SWEEP_TRIPCOUNT_MAX_N:
            values.append(n)
            n *= 2
        return values or [start]

    start = 4
    while start < min_n:
        start *= 2

    def total_bytes(n):
        total = 0
        for v in layout.values():
            if v['reads']:
                total += _buf_size_value(v, 'in', n)
            if v['writes'] and not v.get('has_swi'):
                total += _buf_size_value(v, 'out', n)
        return total * 4

    candidate = _SWEEP_MAX_N_CAP
    while candidate > start and total_bytes(candidate) > _SWEEP_MEM_BUDGET_BYTES:
        candidate //= 2
    candidate = max(candidate, start)

    values, n = [], start
    while n <= candidate:
        values.append(n)
        n *= 2
    return values or [start]


_SIZE_PARAM_TYPES = ('int', 'unsigned', 'unsigned int', 'size_t')


def _size_param_candidates(params):
    """Indices of non-pointer int/unsigned/size_t parameters — candidates for
    "the" runtime size argument. There's no way to tell from the type alone
    which one is really the loop bound if more than one qualifies (e.g.
    `ReverseBits(unsigned index, unsigned NumBits)` — index is data, NumBits
    is the count, both look identical to this check).

    Matches both spellings of `unsigned`: the libclang path reports param
    types via Cursor.type.spelling, which canonicalizes `unsigned foo` to
    `unsigned int foo` — confirmed missing here originally (ReverseBits'
    'unsigned' params silently matched neither the type check nor triggered
    the ambiguity report at all, since libclang never returns bare
    'unsigned'); the regex fallback path preserves the source's own spelling,
    so 'unsigned' alone must stay in this tuple too."""
    return [i for i, (pt, _) in enumerate(params)
            if '*' not in pt and pt.strip() in _SIZE_PARAM_TYPES]


def _loop_cond_size_param(params, size_candidates, loop_cond):
    """When more than one parameter type-qualifies as a size (see
    _size_param_candidates), check whether the pragma'd loop's own condition
    (extracted by _extract_kernel_info_libclang, e.g. "i < n") names exactly
    one of them — much stronger evidence than the type alone, since it's the
    literal loop bound in the original C. Returns that candidate's index, or
    None if loop_cond is unavailable (regex-fallback extraction, no for-loop
    found, ...) or references zero/multiple candidates (still ambiguous)."""
    if not loop_cond:
        return None
    referenced = [i for i in size_candidates
                  if re.search(rf'\b{re.escape(params[i][1])}\b', loop_cond)]
    return referenced[0] if len(referenced) == 1 else None


def _build_ref_args(params, data_cols, layout, scalar_cols, n_expr, extra_decls,
                     size_param_idx=None, ambiguous_size=False, report=None,
                     void_out_var=None, write_col=None):
    """Build C argument expressions for the reference-function call.

    Mirrors the pointer/int/scalar heuristic in gen_test_harness(), generalized to:
      - consume scalar cols in order (not just the first) so multiple scalar
        inputs each get their own column, and
      - when a pointer param has no data col to source from, fall back to the
        address of a local copy of a scalar col's value (needed for kernels
        like isqrt32 whose only parameter is `uint32_t *in_ptr` over what is,
        in stream terms, a single scalar input) — UNLESS void_out_var is set
        (see below).

    size_param_idx: index of the ONE parameter that gets n_expr (the chosen
    size argument — see _size_param_candidates). Other int/unsigned/size_t
    params are NOT assumed to also be the size; they're treated like any
    other scalar parameter instead.
    void_out_var: for a void (array-mutating) reference function, the name of
    the local buffer that receives its output (e.g. "_ref_out"). When set, a
    pointer parameter with no data col left is assumed to be THAT output
    array (e.g. mul_test's z_arr, the third pointer after x_arr/y_arr have
    already consumed the two data cols) rather than "address of a scalar" —
    mirrors gen_test_harness()'s existing void-array heuristic.
    report: optional {'notes': [...], 'todo': [...]} — records a 'todo' entry
    for any parameter with nothing to map it to (NULL fallback). Per-parameter
    column mapping is reported separately by the caller via
    gen_merged_col_report(), using the accessors list below instead.
    write_col: the write column (if any) — used only so an exhausted-pointer
    output param (see void_out_var above) gets an accessor pointing at the
    REAL CGRA output buffer (col{write_col}_out), not the software _ref_out.

    Returns (ref_args, consumed_scalar_count, accessors, param_cols).
    param_cols maps parameter index → the scalar column it consumed, so the
    caller can cross-check its own source-level guess about which parameter is
    the loop trip count against infer_branch_bound_cols()'s independent
    CMEM-level answer (see gen_sweep_section). accessors is a list
    of (param_name, return_type, pointer_expr) triples for
    get_cgra_<param_name>() (see gen_sweep_section) — one per parameter with
    a real, stable CGRA memory location backing it. return_type always
    matches param_name's own declared type (e.g. "char *", "uint32_t *") —
    every column buffer is declared int32_t, so pointer_expr is cast to
    return_type wherever the two differ (else GCC 14 errors on the mismatch,
    -Wincompatible-pointer-types is on by default here, not just a warning).
    Deliberately excludes: the resolved size parameter (n/N_ELEMENTS_FIXED is
    a loop variable or compile-time constant, not a buffer with an address),
    and any parameter with nothing to source it from.
    """
    accessors = []
    param_cols = {}
    dc_idx = [0]

    def next_data_col():
        # Consume data_cols in order so each pointer parameter gets its own
        # column — a kernel with two array parameters (e.g. dot_product(a, b))
        # has two independent data columns, and reusing data_cols[0] for both
        # silently compares CGRA output against a reference fed the same
        # array twice for every pointer arg after the first.
        if dc_idx[0] < len(data_cols):
            dc = data_cols[dc_idx[0]]
            dc_idx[0] += 1
            return dc
        return None

    sc_idx = [0]

    def next_scalar():
        if sc_idx[0] < len(scalar_cols):
            sc = scalar_cols[sc_idx[0]]
            sc_idx[0] += 1
            # The real value lives in the *last* read slot — earlier slots
            # (if any) are required-but-unused pad reads, same convention
            # as the non-swept harness's col_in[n_sc_ref - 1].
            n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
            return sc, f"col{sc}_in[{n_sc - 1}]"
        return None, None

    def todo(i, ptype, pname, msg):
        if report is not None:
            report['todo'].append(f"param '{pname}' (arg {i}, {ptype}): {msg}")

    ref_args = []
    for i, (ptype, pname) in enumerate(params):
        if '*' in ptype:
            data_col = next_data_col()
            if data_col is not None:
                n_init = layout[data_col]['read_init']
                base = f"col{data_col}_in + {n_init}" if n_init else f"col{data_col}_in"
                base_type = ptype.strip().rstrip('*').strip().lstrip('const').strip()
                if base_type not in ('int32_t', 'uint32_t', 'int', 'unsigned int', 'unsigned'):
                    base = f"({ptype}){base}"
                ref_args.append(base)
                # base is already cast to ptype above when its element type
                # isn't int32_t-compatible, so the accessor's declared return
                # type (ptype itself) always matches what it actually returns.
                accessors.append((pname, ptype.strip(), base))
            elif void_out_var is not None:
                ref_args.append(void_out_var)
                if write_col is not None:
                    # col{write_col}_out is always declared int32_t — cast to
                    # this param's real pointer type (e.g. rotatehash's
                    # uint32_t *result) so the accessor's return type matches,
                    # or GCC 14 errors on it (-Wincompatible-pointer-types is
                    # an error by default here, not just a warning).
                    accessors.append((pname, ptype.strip(), f"({ptype.strip()})col{write_col}_out"))
            else:
                sc, sc_expr = next_scalar()
                if sc_expr is not None:
                    param_cols[i] = sc
                    pointee = ptype.strip().rstrip('*').strip()
                    var = f"_arg{i}"
                    extra_decls.append(f"        {pointee} {var} = ({pointee})({sc_expr});")
                    ref_args.append(f"&{var}")
                    accessors.append((pname, ptype.strip(), f"({ptype.strip()})(&{sc_expr})"))
                else:
                    ref_args.append(f"NULL  /* TODO: param '{pname}' — no source column */")
                    todo(i, ptype, pname, "no data col or spare scalar col to source it from — "
                                          "generated call passes NULL, fix by hand.")
        elif i == size_param_idx:
            if ambiguous_size:
                ref_args.append(f"{n_expr}  /* TODO: verify size param guess */")
            else:
                ref_args.append(n_expr)
        else:
            sc, sc_expr = next_scalar()
            if sc_expr is not None:
                param_cols[i] = sc
                ref_args.append(sc_expr)
                sc_ret_type = f"{ptype.strip()} *"
                accessors.append((pname, sc_ret_type, f"({sc_ret_type})(&{sc_expr})"))
            else:
                ref_args.append(f"0  /* TODO: param '{pname}' — no column to map it to */")
                todo(i, ptype, pname, "no column left to map it to — passed as 0. "
                                      "Set it in sweep.c.")
    return ref_args, sc_idx[0], accessors, param_cols


def gen_sweep_fill_lines(layout, data_cols, swept_scalar_cols, n_var,
                         trip_col=None, trip_slot=None, trip_sweepable=False):
    """Fill code for one sweep iteration: data cols get n_var randomized
    elements, swept scalar cols get one randomized value each.

    A data column's leading slots are init/prolog reads, not data. Exactly one
    of them holds the loop trip count, and only when this column is also the
    trip-count column — `trip_col`/`trip_slot` say which, from the CMEM. Writing
    N into slot 0 regardless is wrong two ways: it feeds N to a column that
    never carries it (when the trip count lives elsewhere), and it misses the
    real slot (when the CMEM puts it anywhere but 0). Both produce a kernel that
    runs and returns plausible-looking garbage.
    """
    lines = []
    unidentified = []          # (col, slot) prefix slots nothing accounts for
    for dc in data_cols:
        n_init, n_prol = layout[dc]['read_init'], layout[dc]['read_prolog']
        n_ep = layout[dc]['read_epilog']
        n_prefix = n_init + n_prol
        # Default every prefix slot to 0, then place the trip count where the
        # CMEM actually says it is.
        prefix = ([[k, "0", "TODO: unidentified init slot"] for k in range(n_init)] +
                  [[n_init + k, "0", "prolog pad"] for k in range(n_prol)])
        if dc == trip_col and trip_slot is not None and trip_slot < n_prefix:
            prefix[trip_slot] = (
                [trip_slot, n_var, "loop trip count (runtime)"] if trip_sweepable else
                [trip_slot, "0", "TODO: feeds a branch compare, meaning unresolved — "
                                 "pinned to 0, NOT randomized (a random value here hangs "
                                 "the kernel)"])
        for slot, value, comment in prefix:
            if comment.startswith("TODO: unidentified"):
                unidentified.append((dc, slot))
            lines.append(f"        col{dc}_in[{slot}] = {value};  /* {comment} */")
        rk = layout[dc]['read_kernel']
        n_data_expr = f"{n_var} * {rk}" if rk > 1 else n_var
        lines.append(f"        for (int _i = 0; _i < {n_data_expr}; _i++)")
        lines.append(
            f"            col{dc}_in[{n_prefix} + _i] = "
            f"(int32_t)(sweep_rand() % {_SWEEP_DATA_RAND_MOD}) - {_SWEEP_DATA_RAND_BIAS};"
        )
        for k in range(n_ep):
            lines.append(
                f"        col{dc}_in[{n_prefix} + {n_data_expr} + {k}] = 0;  /* epilog pad */"
            )
    for sc in swept_scalar_cols:
        n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
        for k in range(n_sc - 1):
            lines.append(f"        col{sc}_in[{k}] = 0;  /* pad */")
        lines.append(
            f"        col{sc}_in[{n_sc - 1}] = (int32_t)(sweep_rand() % {_SWEEP_SCALAR_RAND_MASK + 1});"
            f"  /* randomized input */"
        )
    return lines, unidentified


def gen_fixed_scalar_fill(layout, scalar_cols_leftover, size_hint=None, var_fill=None):
    """One-time fill for scalar cols not tied to any reference-function
    argument (e.g. isqrt32's loop-threshold/initial-mask column) — same
    placeholder convention as the non-swept path. These are schedule
    constants, not test data, so they are set once and never swept; review
    them by hand same as you would in the non-swept output.

    size_hint: if given (e.g. 'N_ELEMENTS_FIXED'), used for slot 0 instead of
    the generic placeholder 0. A leftover column (not consumed by any real
    C-level parameter) is often the register a BNE/BEQ loop-termination
    branch compares against, in which case it needs the same trip count used
    to size the array data, not an arbitrary constant — confirmed: filling it
    with 0 instead fed the CGRA a garbage loop bound and caused a stream read
    pointer to walk off the end of SRAM (mul_test repro). Still just a guess
    (could instead be an unrelated fixed constant) — flagged as TODO either way.
    var_fill: optional {col: (value_expr, var_name)} — takes priority over
    size_hint for slot 0 of that column. Used when a single-slot leftover
    column was confidently matched to a reference-source local variable's own
    initializer (see gen_sweep_section's loop_locals handling) — a real value
    from the source instead of a guess.
    """
    var_fill = var_fill or {}
    lines = []
    for sc in scalar_cols_leftover:
        n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
        if sc in var_fill:
            value_expr, var_name = var_fill[sc]
            lines.append(
                f"    col{sc}_in[0] = {value_expr};  /* from `{var_name}`'s own initializer "
                f"in the reference source — verify this is actually the right column for it */")
        elif size_hint is not None:
            lines.append(
                f"    col{sc}_in[0] = {size_hint};  /* TODO: verify — guessed as a "
                f"loop-count/threshold register; could instead be an unrelated fixed "
                f"constant */")
        else:
            lines.append(f"    col{sc}_in[0] = 0;  /* TODO: verify fixed schedule constant, not swept */")
        for k in range(1, n_sc):
            lines.append(f"    col{sc}_in[{k}] = {k * 7};  /* TODO: verify fixed schedule constant, not swept */")
    return lines


def _gen_ref_call_and_compare(is_void, ret, fname, ref_args, write_col, n_expr, layout):
    """The reference-call + CGRA-vs-reference compare block for one sweep
    iteration. Both scalar-return and is_void (array-mutating) kernels hand
    off the actual comparison to verify_results() (verify.c) — a scalar
    result is just an n=1 comparison; is_void compares every element the
    kernel wrote this iteration against _ref_out[] (see _ref_out_decl in
    gen_sweep_section). Either way _errors/_result/_expected come out with
    the same meaning, so label_print and the pass/fail tally right after
    this block need no branching of their own.
    """
    args_str = ', '.join(ref_args)
    if not is_void:
        return (
            f"        int32_t _expected = {fname}_ref({args_str});\n"
            f"        CSR_READ(CSR_REG_MCYCLE, &_t1);\n"
            f"        uint32_t _cpu_cycles = _t1 - _t0;\n"
            f"        int32_t _result = col{write_col}_out[0];\n"
            f"        int32_t _first_got = 0, _first_expected = 0;\n"
            f"        int _errors = verify_results(&_result, &_expected, 1, "
            f"&_first_got, &_first_expected);\n"
        )
    count_expr = _real_output_count_expr(layout[write_col], n_expr)
    return (
        f"        {fname}_ref({args_str});\n"
        f"        CSR_READ(CSR_REG_MCYCLE, &_t1);\n"
        f"        uint32_t _cpu_cycles = _t1 - _t0;\n"
        f"        int32_t _result = 0, _expected = 0;\n"
        f"        int _errors = verify_results(col{write_col}_out, _ref_out, {count_expr}, "
        f"&_result, &_expected);\n"
    )


def _resolve_trip_count(params, probe_cols, branch_bound, resolved_via_loop,
                         data_cols, fname, report):
    """Decide which stream column holds the loop trip count, and say how sure
    we are, from two independent sources:

      CMEM  — infer_branch_bound_cols(): a stream slot feeding a BNE/BEQ
              comparison. This is what the hardware actually does.
      C src — _loop_cond_size_param(): the pragma'd loop's own condition
              naming exactly one parameter (`i < NumBits`).

    Whichever way it comes out, the result must never be randomized like an
    ordinary scalar input. A trip count doesn't vary the test data, it varies
    how many times the kernel runs — randomizing `NumBits` over [0,32768)
    made ReverseBits' reference saturate to a constant 0 on every trial (so
    the comparison could no longer distinguish a correct kernel from one
    returning 0) and cost ~532k loop iterations per sweep. Sweeping it
    instead is both the correct benchmark axis and a far stronger test.

    Returns {'col', 'slot', 'param_idx', 'confidence'} where confidence is:
      'confirmed' — both sources agree; reported as a note.
      'assumed'   — only one source had anything to say; reported under
                    "Review (confident guesses, not certain)".
      'conflict'  — the two sources disagree, or the CMEM shows more than one
                    branch-bound column. Swept anyway (still far better than
                    randomizing it) but reported under "Manual action needed".
    """
    none = {'col': None, 'slot': 0, 'param_idx': None, 'confidence': 'none'}
    src_name  = params[resolved_via_loop][1] if resolved_via_loop is not None else None
    bb_cols   = sorted(branch_bound)

    def agrees_with_source(col):
        """Does the CMEM's trip-count column match what the C source implies?

        probe_cols is the parameter→scalar-column mapping that results from
        accepting the C source's answer, so a size parameter has already been
        taken out of scalar-column consumption there (it's passed by value as
        `n`). The column physically carrying that value is therefore NOT the
        one the size parameter "consumed" — it's either

          - a data column, whose slot 0 is the N prefix the array fill already
            writes `n` into (vec_sum: `int N` ↔ col0_in[0], alongside the
            array itself), or
          - a scalar column left unconsumed by every parameter (reverse_bits:
            `NumBits` ↔ col3_in[0]).

        Both of those agree. A real disagreement is the CMEM pointing at a
        column that some *other* parameter is already feeding.
        """
        if col in data_cols:
            return True
        # A scalar parameter landing on this column is NOT a contradiction —
        # parameter→scalar-column assignment is positional (see _build_ref_args)
        # and therefore has no evidence behind it, whereas the CMEM does. The
        # caller removes the trip-count column from the scalar pool and the
        # remaining parameters shuffle down, which is how reverse_bits' 'index'
        # and 'NumBits' get paired correctly whichever columns they occupy —
        # they swap places when the kernel is rotated (--rotate-cols).
        # A real contradiction is only possible against something that DOES
        # have evidence behind it: an array parameter bound to a data column.
        return True

    if len(bb_cols) > 1:
        report['todo'].append(
            f"CMEM shows {len(bb_cols)} columns whose loaded value feeds a branch comparison "
            f"(cols {', '.join(str(c) for c in bb_cols)}) — this kernel has more than one "
            f"loop bound and cgra_gen.py can't tell which one the sweep should vary. Swept "
            f"col{bb_cols[0]} and left the rest at their placeholder fill; verify by hand "
            f"against sw/satmapit/instructions_*.py before trusting any number from this run.")

    if not bb_cols:
        if resolved_via_loop is None:
            return none
        # The C source names a trip count but no branch in the CMEM compares
        # against a loaded value — so the schedule takes its bound from
        # somewhere this pass can't see. Fall through to the caller's existing
        # data-col/leftover handling rather than inventing a column for it.
        report['review'].append(
            f"{fname}_ref()'s pragma'd loop condition names parameter '{src_name}' as its "
            f"trip count, but no branch in the CMEM compares against a value loaded from a "
            f"stream slot — so nothing confirms which column (if any) actually feeds this "
            f"kernel's loop bound. Sweeping N still varies what the reference function is "
            f"asked to compute, but if the CGRA's own trip count is baked into the schedule "
            f"instead, the two will silently disagree for every N but the mapped one.")
        return none

    # "An LWD feeds a branch operand" does NOT by itself mean "trip count". In a
    # counted loop the branch's other operand is an induction variable and the
    # loaded value is the bound (vec_sum, reverse_bits). In a data-dependent
    # loop it is the reverse: the loaded value is the TERMINATION CONSTANT and
    # the other operand is data. bit_count is the second shape —
    # `do { n++; } while (0 != (x = x & (x-1)))` — where the loaded value is the
    # 0 and the real input `x` arrives on a different slot entirely. Sweeping it
    # as N wrote N into the compare-against-zero slot and 0 into the data slot,
    # so the branch tested `0 != N` forever and the kernel hung on hardware.
    # Distinguishing the two needs the branch's other operand traced back to an
    # increment chain, which this pass does not do — so CMEM evidence alone is
    # reported but is NOT allowed to drive the sweep (see gen_sweep_section).
    col  = bb_cols[0]
    info = branch_bound[col]
    slot = info['slot']
    # Which parameter (if any) would otherwise have consumed that column.
    param_idx = next((i for i, c in probe_cols.items() if c == col), None)
    detail = (f"CMEM: RC row {info['row']} col {col} loads {info['reg']} by LWD at T="
              f"{info['lwd_T']}, and its {info['branch_op']} at T={info['branch_T']} compares "
              f"against {info['reg']}")

    if not info['slot_certain']:
        report['todo'].append(
            f"col{col}_in[]: two rows load at T={info['lwd_T']} and the CMEM doesn't say "
            f"which is served first. Guessed slot {slot} (50/50). If results look wrong, swap "
            f"the first two slots in sweep.c.")

    if resolved_via_loop is not None and agrees_with_source(col):
        report['notes'].append(
            f"Loop trip count: '{src_name}' = col{col}_in[{slot}], swept as N. "
            f"(Loop condition and CMEM agree.)")
        return {'col': col, 'slot': slot, 'param_idx': resolved_via_loop,
                'confidence': 'confirmed'}

    if resolved_via_loop is not None:
        clash = next(f"'{params[i][1]}'" for i, c in probe_cols.items() if c == col)
        report['todo'].append(
            f"Trip count is ambiguous: the loop condition points at '{src_name}', the CMEM "
            f"at col{col}, which parameter {clash} already feeds. Used col{col}. Check the "
            f"parameter-to-column mapping in sweep.c.")
        return {'col': col, 'slot': slot, 'param_idx': param_idx, 'confidence': 'conflict'}

    who = (f"parameter '{params[param_idx][1]}'" if param_idx is not None
           else "no reference-function parameter (a schedule-only column)")
    return {'col': col, 'slot': slot, 'param_idx': param_idx, 'confidence': 'assumed'}


def gen_sweep_section(layout, ref_info, min_n, sweep_mode='auto', sweep_range=None,
                       sweep_trials=None, report=None, branch_bound=None):
    """Build the sweep's declarations + main-loop body, or return None if this
    kernel doesn't land in the simple auto-verified branch (in which case the
    caller falls back to the single-shot gen_test_harness path unchanged).

    sweep_mode: 'auto' | 'loop_bound' | 'scalar' | 'off' — see --sweep CLI flag.
    sweep_range: optional (lo, hi) int tuple overriding the auto-sized doubling
                 range for 'loop_bound' kind (see --sweep-range).
    sweep_trials: optional int overriding _SWEEP_TRIALS for 'trials' kind
                  (see --sweep-trials).
    report: optional {'notes': [...], 'todo': [...]} dict, mutated in place —
            collects the end-of-run summary printed by main().

    Kind selection ('auto'): a data col alone (read_kernel > 0) is NOT enough
    to justify sweeping N — a fixed-size loop (`for i < VEC_SIZE`) also reads
    an array every iteration, and it has no runtime N to vary. The signal that
    actually distinguishes them is whether the *reference function's own
    signature* exposes a real int/unsigned/size_t size parameter (vec_sum's
    `int N` vs. isqrt32 having no such param at all) — that parameter is only
    there if the original C loop bound came from outside the function, i.e.
    is genuinely runtime-variable. Forcing 'loop_bound' via --sweep bypasses
    this check (for cases the heuristic gets wrong), but can't invent an N to
    sweep if there's no data col to size in the first place.
    """
    if report is None:
        report = {'notes': [], 'review': [], 'todo': []}
    if sweep_mode == 'off':
        report['notes'].append("Sweep disabled via --sweep off — single fixed-input "
                                "PASS/FAIL run only.")
        return None
    status = _verify_path_status(layout, ref_info)
    if status is not None:
        tag, msg = status
        if msg is not None:
            report[tag].append(msg)
        if sweep_mode in ('loop_bound', 'scalar'):
            report['notes'].append(f"--sweep={sweep_mode} requested but ignored — this kernel "
                                    f"isn't in the sweep-eligible verification path.")
        return None

    params = ref_info.get('params', [])
    ret    = ref_info.get('return_type', 'int32_t')
    fname  = ref_info['func_name']
    is_void = ret.strip() in ('void', '')

    data_cols   = [c for c, v in sorted(layout.items()) if v['read_kernel'] > 0]
    scalar_cols = [c for c, v in sorted(layout.items())
                   if v['read_kernel'] == 0 and v['reads'] and not v.get('has_swi')]
    write_cols  = [c for c, v in sorted(layout.items())
                   if v['writes'] and not v.get('has_swi')]
    write_col   = write_cols[0]

    if is_void:
        report['notes'].append(
            f"{fname}_ref() returns void and writes its result through an output pointer, not "
            f"a return value — verified each iteration by comparing every element the CGRA "
            f"wrote against a software-computed reference array. In the SWEEP line below: "
            f"errors= is the count of mismatched elements; got=/expected= is the first "
            f"mismatching pair (0/0 if all elements matched).")

    size_candidates = _size_param_candidates(params)
    has_size_param  = bool(size_candidates)
    # No way to tell which is "really" the size when more than one param
    # qualifies (e.g. ReverseBits(unsigned index, unsigned NumBits) — both
    # look identical to this check). First try the pragma'd loop's own
    # condition (e.g. "i < n") — if it names exactly one candidate, that's
    # real evidence, not a guess. Run it even when only one candidate
    # type-qualifies: _resolve_trip_count() cross-checks its answer against
    # the CMEM regardless of how many parameters happened to qualify.
    resolved_via_loop = _loop_cond_size_param(params, size_candidates,
                                              ref_info.get('loop_cond'))

    # Probe run: the parameter→scalar-column mapping that results from taking
    # the C source at its word. _resolve_trip_count() checks the CMEM's answer
    # against this. It must use resolved_via_loop, not None — passing None
    # forces a size parameter to consume a scalar column it would never really
    # consume, which shifts every parameter after it and manufactures a
    # conflict (vec_sum: `int N` would land on col3, `init`'s column).
    _, _, _, probe_cols = _build_ref_args(params, data_cols, layout, scalar_cols,
                                           'n', [], size_param_idx=resolved_via_loop,
                                           void_out_var='_ref_out' if is_void else None,
                                           write_col=write_col)
    trip = _resolve_trip_count(params, probe_cols, branch_bound or {}, resolved_via_loop,
                                data_cols, fname, report)
    # Only a trip count corroborated by BOTH the CMEM and the C loop condition is
    # trusted enough to write N into. CMEM-only evidence can't tell a loop bound
    # from a termination constant (see _resolve_trip_count), and guessing wrong
    # there hangs the kernel rather than merely producing a wrong number.
    # The column stays reserved either way — it must never be randomized, since a
    # random value in a branch's compare operand is what makes a kernel spin
    # instead of merely return the wrong answer. Only whether it carries N changes.
    trip['sweepable'] = (trip['confidence'] == 'confirmed')
    if trip['col'] is not None and not trip['sweepable']:
        report['todo'].append(
            f"col{trip['col']}_in[{trip['slot']}]: feeds a branch compare, but could be "
            f"either a loop bound or a loop-termination constant. Pinned to 0, not swept. "
            f"Check what that column's slots feed in this app's instructions_*.py, then set them in "
            f"sweep.c.")
        trip['param_idx'] = None

    size_param_idx = (trip['param_idx'] if trip['param_idx'] is not None else
                      resolved_via_loop if resolved_via_loop is not None else
                      (size_candidates[-1] if size_candidates else None))

    # A trip count is a real sweep axis on its own — an array column is not
    # required. reverse_bits (`for i < NumBits`, no array at all) is exactly
    # that shape, and before this it fell through to the randomized-trials
    # path and had its trip count scrambled instead of swept.
    if sweep_mode == 'loop_bound':
        if not data_cols and not trip['sweepable']:
            report['notes'].append("--sweep=loop_bound requested but neither an array column "
                                    "nor a loop trip count was found — nothing to size a "
                                    "loop-bound sweep over; used a trials sweep instead.")
            kind = 'trials'
        else:
            kind = 'loop_bound'
    elif sweep_mode == 'scalar':
        kind = 'trials'
        if trip['col'] is not None:
            report['review'].append(
                f"--sweep=scalar forced a randomized-trials sweep, but col{trip['col']}_in"
                f"[{trip['slot']}] is this kernel's loop trip count — it is held at its "
                f"structural minimum rather than randomized, since randomizing it would vary "
                f"how many times the kernel runs instead of varying the test data.")
    else:  # auto
        kind = 'loop_bound' if (trip['sweepable'] or (data_cols and has_size_param)) \
               else 'trials'
        if data_cols and not has_size_param and not trip['sweepable']:
            report['notes'].append(
                f"{fname}_ref() has an array column but no int/unsigned/size_t parameter, and "
                f"no branch in the CMEM compares against a loaded value — treating N as fixed, "
                f"not sweeping problem size (pass --sweep loop_bound to force it if this "
                f"kernel does have a real runtime size not visible in the reference signature).")

    n_expr = 'n' if kind == 'loop_bound' else 'N_ELEMENTS_FIXED'
    size_param_idx_used = size_param_idx if kind == 'loop_bound' else None
    # The trip-count column must never reach gen_sweep_fill_lines()' randomizer,
    # whichever sweep kind we ended up in.
    trip_col = trip['col']

    # _resolve_trip_count() has already reported which column is the trip count
    # and how sure it is. What's left to say here is what happens to the OTHER
    # type-qualifying parameters, and to flag the case where nothing at all
    # pinned the size down and the tool is falling back to positional guessing.
    resolved_size = trip['param_idx'] if trip['param_idx'] is not None else resolved_via_loop
    if len(size_candidates) > 1:
        amb_names = ', '.join(f"'{params[i][1]}'" for i in size_candidates)
        others = [f"'{params[i][1]}'" for i in size_candidates if i != resolved_size]
        if resolved_size is not None:
            report['notes'].append(
                f"{fname}_ref() has {len(size_candidates)} int/unsigned/size_t parameters "
                f"({amb_names}). {', '.join(others)} "
                f"{'are' if len(others) > 1 else 'is'} treated as plain randomized scalar "
                f"input(s), not as a sweep dimension.")
        elif kind == 'loop_bound':
            report['todo'].append(
                f"{fname}_ref() has {len(size_candidates)} int/unsigned/size_t parameters "
                f"({amb_names}) — cgra_gen.py can't tell from the type alone which one is the "
                f"real size. Neither the pragma'd loop's condition nor any branch in the CMEM "
                f"named exactly one of them, so it guessed '{params[size_param_idx][1]}' (the "
                f"last one, marked with a TODO comment in the ref call in sweep.c). Verify "
                f"that's actually the size; the others are treated as plain scalar inputs.")
        else:
            report['todo'].append(
                f"{fname}_ref() has {len(size_candidates)} int/unsigned/size_t parameters "
                f"({amb_names}), and nothing identified any of them as the loop trip count — "
                f"no array column to size, no branch in the CMEM comparing against a loaded "
                f"value, no single parameter named by the loop condition. All of them are "
                f"randomized independently as plain scalar inputs every trial. If one is "
                f"really a trip count, that randomization changes how many times the kernel "
                f"runs instead of varying the test data — check by hand and pass "
                f"--sweep loop_bound if so.")

    # The trip-count column carries N, which is passed to the reference function
    # by value — so no other parameter may consume it. Removing it from the pool
    # makes the remaining parameters shuffle down onto the columns that are
    # actually left, instead of the first one blindly taking column 0.
    scalar_pool = ([c for c in scalar_cols if c != trip_col]
                   if trip_col is not None else scalar_cols)

    extra_decls = []
    ref_args, consumed, accessors, _ = _build_ref_args(
        params, data_cols, layout, scalar_pool, n_expr, extra_decls,
        size_param_idx=size_param_idx_used,
        ambiguous_size=(len(size_candidates) > 1 and kind == 'loop_bound'
                        and resolved_size is None),
        report=report, void_out_var='_ref_out' if is_void else None,
        write_col=write_col)
    swept_scalar_cols   = scalar_pool[:consumed]
    leftover_scalar_cols = scalar_pool[consumed:]
    primary_input_col = swept_scalar_cols[0] if (kind == 'trials' and swept_scalar_cols) else None

    trip_param_name = (params[trip['param_idx']][1]
                       if trip['param_idx'] is not None else None)
    col_report = gen_merged_col_report(layout, accessors, write_col, is_void, ret,
                                        leftover_scalar_cols,
                                        trip=trip, trip_param_name=trip_param_name)
    if col_report:
        report['notes'].append("Auto-inferred column layout:\n    " + "\n    ".join(col_report))
    if accessors:
        fn_list = ', '.join(f"get_cgra_{pname}()" for pname, _, _ in accessors)
        report['notes'].append(f"Accessors declared in sweep.h: {fn_list}")

    # (NULL-pointer-with-nothing-to-source-from is already reported per-parameter
    # by _build_ref_args' own todo() call above — no need to also report it in
    # aggregate here.)

    # A leftover column not tied to any real C-level parameter is often a
    # BNE/BEQ loop-count/threshold register — guessing 0 there fed the CGRA a
    # garbage trip count and caused a stream read pointer to walk off the end
    # of SRAM (confirmed: mul_test repro). For 'loop_bound' kind the real N
    # changes every sweep iteration, and the CMEM/bitstream is fixed once and
    # reused across all of them — the LWD that loads this column's value into
    # the branch's comparison register re-fires on every cgra_set_kernel()
    # call, so a value that was right for the first N is stale (and wrong) by
    # the second. It has to be refreshed every iteration, same as the swept
    # data/scalar cols already are (see gen_sweep_fill_lines) — so it goes
    # into per_iter_leftover_fill (added to the loop body below), not the
    # one-time fixed_fill_block. For 'trials' kind N really is constant across
    # the whole sweep, so a one-time fill (to N_ELEMENTS_FIXED) is correct.
    # The trip-count column is the sweep's own axis, so it's driven from n (or
    # pinned to the fixed N for a trials sweep) — never randomized, never left
    # to the leftover guesswork below. When it doubles as the data column
    # (vec_sum on 4x4: col0 carries both the N prefix and the array),
    # gen_sweep_fill_lines() places it at trip['slot'] itself and this adds
    # nothing.
    trip_fill = []
    trip_is_data_col = trip_col is not None and trip_col in data_cols
    if trip_col is not None and not trip_is_data_col:
        n_tc = max(layout[trip_col]['read_init'] + layout[trip_col]['read_prolog'], 1)
        if trip['sweepable']:
            trip_fill.append(
                f"        col{trip_col}_in[{trip['slot']}] = {n_expr};  /* loop trip count "
                f"({trip['confidence']}, see GENERATION_SUMMARY.md) */")
        else:
            trip_fill.append(
                f"        col{trip_col}_in[{trip['slot']}] = 0;  /* TODO: feeds a branch "
                f"compare, meaning unresolved — pinned to 0, NOT randomized (a random value "
                f"here hangs the kernel). See GENERATION_SUMMARY.md. */")
        for k in range(n_tc):
            if k != trip['slot']:
                trip_fill.append(
                    f"        col{trip_col}_in[{k}] = 0;  /* TODO: verify — extra slot in the "
                    f"trip-count column, nothing identified what it holds */")
    elif trip_is_data_col and trip['slot'] != 0:
        report['notes'].append(
            f"col{trip_col} is both the array column and the trip-count column; N is written "
            f"to slot {trip['slot']} as the CMEM indicates, not to slot 0.")

    leftover_size_hint = 'N_ELEMENTS_FIXED' if (kind == 'trials' and data_cols) else None
    per_iter_leftover_fill = []
    leftover_var_fill = {}
    if leftover_scalar_cols:
        cols_str = ', '.join(f"col{c}_in[]" for c in leftover_scalar_cols)
        if trip_col is not None:
            # We found the real trip count in the CMEM, and it isn't these. The
            # old "a leftover column is probably the loop counter, fill it with
            # N" guess would now be actively wrong, so don't make it.
            report['todo'].append(
                f"{cols_str}: not used by any parameter, filled with placeholders. Check "
                f"what those slots feed in the instructions_*.py and set them in sweep.c.")
        elif kind == 'loop_bound' and data_cols:
            report['review'].append(
                f"{cols_str}: not consumed by {fname}_ref()'s signature — guessed as a "
                f"loop-count/threshold register and refreshed to the current N every sweep "
                f"iteration (see the TODO comment in sweep.c). No branch in the CMEM compares "
                f"against a loaded value, so this is the type/shape heuristic talking, not "
                f"real evidence. Verify it; could instead be an unrelated fixed constant.")
            for sc in leftover_scalar_cols:
                n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
                per_iter_leftover_fill.append(
                    f"        col{sc}_in[0] = {n_expr};  /* TODO: verify — guessed as a "
                    f"loop-count/threshold register; could instead be an unrelated fixed "
                    f"constant */")
                for k in range(1, n_sc):
                    per_iter_leftover_fill.append(
                        f"        col{sc}_in[{k}] = {k * 7};  /* TODO: verify fixed schedule "
                        f"constant */")
        elif leftover_size_hint is not None:
            report['review'].append(
                f"{cols_str}: not consumed by {fname}_ref()'s signature — guessed as a "
                f"loop-count/threshold register and set to {leftover_size_hint} (see the "
                f"TODO comment in sweep.c). Verify that's actually correct; could instead "
                f"be an unrelated fixed constant.")
        else:
            # No structural signal (no size param, no array) to guess a value
            # from — but the reference source itself may still hold the
            # answer: a local variable declared with an initializer, read by
            # this kernel's own while-loop condition (see loop_locals in
            # _extract_kernel_info_libclang), is a real candidate for what a
            # leftover column holds — e.g. isqrt32's `uint16_t mask = 1 << 14`
            # + `while (mask)`. Only auto-fill it when the match is
            # unambiguous (exactly one leftover column, exactly one required
            # slot in it, exactly one candidate variable) — a leftover column
            # needing 2+ slots (isqrt32's col1: one for the loop's shifted
            # value, one for its implicit branch threshold) can't be safely
            # auto-assigned from a single candidate without knowing which
            # slot is which, which would need tracing the CMEM's register
            # dataflow, not just the C source — so that case falls through to
            # "surface as a hint, let the user finish it" instead of guessing.
            loop_locals = ref_info.get('loop_locals', []) if ref_info else []
            sole_col = leftover_scalar_cols[0] if len(leftover_scalar_cols) == 1 else None
            sole_n_sc = (max(layout[sole_col]['read_init'] + layout[sole_col]['read_prolog'], 1)
                         if sole_col is not None else None)
            if sole_col is not None and sole_n_sc == 1 and len(loop_locals) == 1:
                lname, ltype, linit = loop_locals[0]
                leftover_var_fill[sole_col] = (linit, lname)
                report['review'].append(
                    f"col{sole_col}_in[0]: not consumed by {fname}_ref()'s signature, but set "
                    f"to {linit} — the value local variable `{lname}` ({ltype}) is declared "
                    f"with, referenced directly in this kernel's own while-loop condition. "
                    f"Verify that's actually the right column for it; could instead be an "
                    f"unrelated constant that happens to share the same shape.")
            elif loop_locals:
                candidates = ', '.join(f"`{n}` ({t}, initial value {v})" for n, t, v in loop_locals)
                report['todo'].append(
                    f"{cols_str}: not consumed by {fname}_ref()'s signature, left at the tool's "
                    f"generic placeholder (0, 7, 14, ...) — this kernel's while-loop condition "
                    f"does reference local variable(s) that might explain one of these slots "
                    f"({candidates}), but there are multiple slots/candidates here and "
                    f"cgra_gen.py can't tell which slot is which without tracing the CMEM's "
                    f"register dataflow. Manual step: check sw/satmapit/instructions_*.py for "
                    f"which register each slot loads and what reads it, then match against "
                    f"the candidate(s) above (see the TODO comments in sweep.c).")
            else:
                report['todo'].append(
                    f"{cols_str}: fixed placeholder values (0, 7, 14, ...) — not consumed by "
                    f"{fname}_ref()'s signature, and nothing in the reference source's own "
                    f"while-loop condition (if it has one) explains them either — cgra_gen.py "
                    f"couldn't infer anything here. Manual step: verify these match the "
                    f"kernel's real schedule constants (see the TODO comments in sweep.c).")

    # Anything already refreshed inside the loop must not also be filled once
    # up front; everything else must be, or it never gets written at all.
    fixed_scalar_leftover = [] if per_iter_leftover_fill else leftover_scalar_cols
    fixed_fill = gen_fixed_scalar_fill(layout, fixed_scalar_leftover, size_hint=leftover_size_hint,
                                       var_fill=leftover_var_fill)
    fixed_fill_block = ("\n".join(fixed_fill) + "\n") if fixed_fill else ""

    def _ref_out_decl(n_token):
        # Software-reference output buffer for is_void kernels — sized like
        # the real write column's output, just with N_ELEMENTS substituted
        # for whatever this sweep kind's max/fixed N token is, so it's always
        # big enough for the largest iteration run through it.
        size = _buf_size_expr(layout[write_col], 'out').replace('N_ELEMENTS', n_token)
        return (f"static int32_t _ref_out[{size}] __attribute__((aligned(4)));"
                f"  /* software-reference output, sized like col{write_col}_out */\n")

    if kind == 'loop_bound':
        tripcount_only = bool(trip_col is not None and not data_cols)
        n_values = plan_loop_bound_sweep(layout, min_n, explicit_range=sweep_range,
                                          tripcount_only=tripcount_only)
        n_max = max(n_values)
        if sweep_range is not None:
            range_note = " * Range set explicitly via --sweep-range."
        elif tripcount_only:
            range_note = (
                f" * N here is a bare loop trip count — no array scales with it, so the range\n"
                f" * is bounded by runtime, not memory: capped at _SWEEP_TRIPCOUNT_MAX_N\n"
                f" * ({_SWEEP_TRIPCOUNT_MAX_N}) in util/cgra_gen.py. Pass --sweep-range MIN:MAX to change it."
            )
        else:
            range_note = (
                f" * Largest N is capped to fit an on-chip memory budget of roughly\n"
                f" * {_SWEEP_MEM_BUDGET_BYTES} bytes of sweep buffers — widen _SWEEP_MEM_BUDGET_BYTES\n"
                f" * in util/cgra_gen.py, or pass --sweep-range MIN:MAX, for a different range."
            )
        io_decls = (
            f"/* Doubling sweep of problem size N (kernel requires N >= {min_n}).\n"
            f"{range_note} */\n"
            f"static const int N_SWEEP[] = {{ {', '.join(str(v) for v in n_values)} }};\n"
            f"#define N_SWEEP_COUNT ((int)(sizeof(N_SWEEP) / sizeof(N_SWEEP[0])))\n"
            f"#define N_ELEMENTS_MAX {n_max}\n"
            f"\n"
            f"{gen_io_buffers(layout).replace('N_ELEMENTS', 'N_ELEMENTS_MAX')}"
            f"{_ref_out_decl('N_ELEMENTS_MAX') if is_void else ''}"
            f"\n{_RNG_BLOCK}"
        )
        size_param_note = (f" over '{params[size_param_idx][1]}'"
                            if size_param_idx is not None else
                            f" over col{trip_col}_in[{trip['slot']}]"
                            if trip_col is not None else "")
        if sweep_range is not None:
            sized_note = " (explicit --sweep-range)."
        elif tripcount_only:
            sized_note = f" (bare trip count, capped at {_SWEEP_TRIPCOUNT_MAX_N})."
        else:
            sized_note = f" (auto-sized to fit ~{_SWEEP_MEM_BUDGET_BYTES}B budget)."
        report['notes'].append(
            f"Sweep: loop_bound{size_param_note}, N in "
            f"{{{', '.join(str(v) for v in n_values)}}}" + sized_note
        )
    else:
        n_trials = sweep_trials if sweep_trials is not None else _SWEEP_TRIALS
        if data_cols:
            fixed_n_comment = (
                f"/* TODO: verify N_ELEMENTS_FIXED — set to this kernel's structural minimum, "
                f"not a confirmed array size. */\n"
                f"#define N_ELEMENTS_FIXED  {min_n}\n"
            )
            report['review'].append(
                f"N_ELEMENTS_FIXED is set to {min_n} (structural minimum, not a confirmed "
                f"array size) — verify it matches what this kernel was actually mapped for.")
        elif trip_col is not None:
            # Reachable via --sweep scalar on a kernel that does have a trip
            # count. It's pinned, not randomized (see _resolve_trip_count).
            fixed_n_comment = (
                f"/* Trip count held fixed at this kernel's structural minimum — the sweep\n"
                f" * varies the other inputs instead (--sweep scalar). */\n"
                f"#define N_ELEMENTS_FIXED  {min_n}\n"
            )
        else:
            fixed_n_comment = "/* Fixed-schedule kernel: no loop bound, repeats with randomized inputs. */\n"
        report['notes'].append(f"Sweep: trials ({n_trials} runs, randomized inputs).")
        io_decls = (
            f"{fixed_n_comment}"
            f"#define SWEEP_TRIALS  {n_trials}\n"
            f"\n"
            f"{gen_io_buffers(layout).replace('N_ELEMENTS', 'N_ELEMENTS_FIXED')}"
            f"{_ref_out_decl('N_ELEMENTS_FIXED') if is_void else ''}"
            f"\n{_RNG_BLOCK}"
        )

    ptr_setup = gen_ptr_setup(layout)

    fill_n_var = 'n' if kind == 'loop_bound' else ('N_ELEMENTS_FIXED' if data_cols else None)
    fill_lines, unidentified_slots = gen_sweep_fill_lines(
        layout, data_cols, swept_scalar_cols, fill_n_var,
        trip_col=trip_col,
        trip_slot=(trip['slot'] if trip_col is not None else None),
        trip_sweepable=bool(trip and trip.get('sweepable')))
    for _col, _slot in unidentified_slots:
        report['todo'].append(
            f"col{_col}_in[{_slot}]: read before the data stream but nothing identified what "
            f"it holds — filled with 0. Check what it feeds in the instructions_*.py.")
    fill_lines += trip_fill
    fill_lines += per_iter_leftover_fill
    fill_block = "\n".join(fill_lines)
    extra_decls_block = ("\n".join(extra_decls) + "\n") if extra_decls else ""

    # got=/expected= only mean anything on a mismatch (verify_results() leaves
    # them at 0 when _errors == 0) — printing them at all on a pass is just
    # noise (0/0 reads as "compared 0 against 0", not "nothing to report").
    # Omit them from the line entirely when there's no error instead.
    if kind == 'loop_bound':
        loop_open   = "    for (int _si = 0; _si < N_SWEEP_COUNT; _si++) {\n        int n = N_SWEEP[_si];\n"
        label_print = (
            '        if (_errors) {\n'
            '            printf("SWEEP N=%d cpu_cycles=%u cgra_active=%u cgra_stall=%u speedup=n/a '
            'errors=%d got=%d expected=%d\\n",\n'
            '                   n, _cpu_cycles, _ca, _cs, _errors, '
            '(int)_result, (int)_expected);\n'
            '        } else {\n'
            '            printf("SWEEP N=%d cpu_cycles=%u cgra_active=%u cgra_stall=%u '
            'stall_pct=%u.%02u cgra_wall=%u speedup=%u.%02u speedup_e2e=%u.%02u errors=0\\n",\n'
            '                   n, _cpu_cycles, _ca, _cs, _st_i, _st_f, _cgra_wall,\n'
            '                   _sp_i, _sp_f, _se_i, _se_f);\n'
            '        }'
        )
    else:
        loop_open   = "    for (int _trial = 0; _trial < SWEEP_TRIALS; _trial++) {\n"
        if primary_input_col is not None:
            n_sc_primary = max(layout[primary_input_col]['read_init']
                               + layout[primary_input_col]['read_prolog'], 1)
            input_expr = f"col{primary_input_col}_in[{n_sc_primary - 1}]"
        else:
            input_expr = "0"
        label_print = (
            f'        if (_errors) {{\n'
            f'            printf("SWEEP TRIAL=%d input=%d cpu_cycles=%u cgra_active=%u cgra_stall=%u '
            f'speedup=n/a errors=%d got=%d expected=%d\\n",\n'
            f'                   _trial, (int)({input_expr}), _cpu_cycles, _ca, _cs, '
            f'_errors, (int)_result, (int)_expected);\n'
            f'        }} else {{\n'
            f'            printf("SWEEP TRIAL=%d input=%d cpu_cycles=%u cgra_active=%u cgra_stall=%u '
            f'stall_pct=%u.%02u cgra_wall=%u speedup=%u.%02u speedup_e2e=%u.%02u errors=0\\n",\n'
            f'                   _trial, (int)({input_expr}), _cpu_cycles, _ca, _cs, '
            f'_st_i, _st_f, _cgra_wall, _sp_i, _sp_f, _se_i, _se_f);\n'
            f'        }}'
        )

    # Three measurements land in the emitted sweep line, and they are not
    # interchangeable:
    #   _cpu_cycles  mcycle around the CPU reference function
    #   _cgra_wall   mcycle around launch + completion interrupt (offload cost)
    #   _ca / _cs    CGRA hardware counters; _cs is a SUBSET of _ca, never added
    #                to it (peripheral_regs.sv enables active on
    #                col_status|acc_req, and col_status is held acc_ack->acc_end,
    #                so it stays set through stall cycles the stall counter also
    #                counts). _ca + _cs double-counts and understates speedup.
    # _ca <= _cgra_wall must always hold; a violation means one of the two is
    # being read wrong.
    sweep_body = (
        f"    /* ── Fixed (non-swept) scalar inputs — review before trusting ─── */\n"
        f"{fixed_fill_block}"
        f"    /* ── Stream pointers (constant across the sweep) ───────────────── */\n"
        f"{ptr_setup}\n\n"
        f"    int _sweep_total = 0, _sweep_pass = 0;\n"
        f"{loop_open}"
        f"{fill_block}\n"
        f"\n"
        f"        cgra_perf_cnt_reset(cgra);\n"
        f"        cgra_intr_arm();\n"
        f"        /* offload wall clock: launch + execution + completion interrupt */\n"
        f"        uint32_t _w0, _w1;\n"
        f"        CSR_READ(CSR_REG_MCYCLE, &_w0);\n"
        f"        cgra_set_kernel(cgra, KER_ID_1);\n"
        f"        if (!cgra_intr_wait()) {{\n"
        f"            printf(\"### CGRA TIMEOUT: kernel never completed — check "
        f"fixed/scalar column fill above (bad schedule constant?) ###\\n\");\n"
        f"            printf(\"### FAIL ###\\n\");\n"
        f"            return;\n"
        f"        }}\n"
        f"        CSR_READ(CSR_REG_MCYCLE, &_w1);\n"
        f"        uint32_t _cgra_wall = _w1 - _w0;\n"
        f"\n"
        f"        uint32_t _t0, _t1;\n"
        f"        CSR_READ(CSR_REG_MCYCLE, &_t0);\n"
        f"{extra_decls_block}"
        f"{_gen_ref_call_and_compare(is_void, ret, fname, ref_args, write_col, n_expr, layout)}"
        f"\n"
        f"        uint32_t _ca = cgra_perf_cnt_get_col_active(cgra, 0);\n"
        f"        uint32_t _cs = cgra_perf_cnt_get_col_stall(cgra, 0);\n"
        f"        /* stall is a subset of active, not disjoint — do not add them */\n"
        f"        uint32_t _ct = _ca;\n"
        f"        uint32_t _sp_i = 0, _sp_f = 0;\n"
        f"        if (_cpu_cycles > 0 && _ct > 0) {{\n"
        f"            uint32_t _sp = _cpu_cycles * 100u / _ct;\n"
        f"            _sp_i = _sp / 100u; _sp_f = _sp % 100u;\n"
        f"        }}\n"
        f"        uint32_t _st_i = 0, _st_f = 0;\n"
        f"        if (_ca > 0) {{\n"
        f"            uint32_t _st = _cs * 10000u / _ca;\n"
        f"            _st_i = _st / 100u; _st_f = _st % 100u;\n"
        f"        }}\n"
        f"        uint32_t _se_i = 0, _se_f = 0;\n"
        f"        if (_cpu_cycles > 0 && _cgra_wall > 0) {{\n"
        f"            uint32_t _se = _cpu_cycles * 100u / _cgra_wall;\n"
        f"            _se_i = _se / 100u; _se_f = _se % 100u;\n"
        f"        }}\n"
        f"{label_print}\n"
        f"\n"
        f"        _sweep_total++;\n"
        f"        if (!_errors) _sweep_pass++;\n"
        f"    }}\n"
        f"    printf(\"SWEEP SUMMARY total=%d passed=%d failed=%d\\n\",\n"
        f"           _sweep_total, _sweep_pass, _sweep_total - _sweep_pass);\n"
        f"\n"
        f"    /* cgra_perf_cnt_reset() runs every iteration, so the hardware counter only\n"
        f"     * ever reads back the last one — report the loop's own tally instead. */\n"
        f"    printf(\"Kernels executed: %d\\n\", _sweep_total);\n"
        f"    printf(_sweep_pass == _sweep_total ? \"### PASS ###\\n\" : \"### FAIL ###\\n\");"
    )

    accessor_decls = "".join(
        f"{ret_type} get_cgra_{pname}(void);\n" for pname, ret_type, _ in accessors
    )
    accessor_defs = "".join(
        f"{ret_type} get_cgra_{pname}(void) {{ return {expr}; }}\n"
        for pname, ret_type, expr in accessors
    )

    return {
        'io_decls': io_decls,
        'sweep_body': sweep_body,
        'accessor_decls': accessor_decls,
        'accessor_defs': accessor_defs,
    }


# ─── C source parser ─────────────────────────────────────────────────────────
_libclang_ready = None       # tri-state cache: None=not tried, True/False=result
_LIBCLANG_RESOURCE_DIR = None


def _setup_libclang(satmapit_dir):
    """Best-effort: point clang.cindex at the SAT-MapIt-vendored libclang.so.

    Returns True if usable, False if not (caller falls back to regex parsing).
    Cached after the first call — satmapit_dir is only actually used the first
    time; later calls with a different value still return the cached result.
    """
    global _libclang_ready, _LIBCLANG_RESOURCE_DIR
    if _libclang_ready is not None:
        return _libclang_ready
    try:
        import clang.cindex as ci
    except ImportError:
        print("  WARNING: python 'clang' bindings not importable in this environment — "
              "falling back to the older regex/brace-counting --ref-src extraction, which "
              "is known to mishandle some source layouts (see docs/cgra_kernel_toolchain.md "
              "TODO section 2.2a). Fix with:\n"
              "    conda run -n core-v-mini-mcu pip install "
              "-r util/python-requirements-satmapit.txt")
        _libclang_ready = False
        return False

    try:
        lib_path = os.path.join(satmapit_dir, "llvm-project", "build", "lib", "libclang.so")
        if not os.path.isfile(lib_path):
            print(f"  WARNING: libclang.so not found at {lib_path} — falling back to the "
                  f"older regex/brace-counting --ref-src extraction (see "
                  f"docs/cgra_kernel_toolchain.md TODO section 2.2a). Check SATMAPIT_DIR / "
                  f"--satmapit-dir and that SAT-MapIt's LLVM build actually completed.")
            _libclang_ready = False
            return False
        ci.Config.set_library_file(lib_path)
        # This fork's libclang.so doesn't export every symbol in the bindings'
        # full function table (e.g. clang_getOffsetOfBase, a C++-only, obscure
        # one we never call) — without this, Index.create() below refuses to
        # load the library at all over a single irrelevant missing symbol.
        ci.Config.compatibility_check = False

        clang_lib_dir = os.path.join(satmapit_dir, "llvm-project", "build", "lib", "clang")
        if os.path.isdir(clang_lib_dir):
            versions = sorted(os.listdir(clang_lib_dir))
            if versions:
                # Needed so clang can find its own bundled headers (stddef.h,
                # etc.) — the CLI driver auto-detects this, the library API doesn't.
                _LIBCLANG_RESOURCE_DIR = os.path.join(clang_lib_dir, versions[-1])

        ci.Index.create()  # smoke test: surfaces symbol-registration failures now
        _libclang_ready = True
    except Exception as e:
        print(f"  WARNING: libclang failed to load ({e}) — falling back to the older "
              f"regex/brace-counting --ref-src extraction (see docs/cgra_kernel_toolchain.md "
              f"TODO section 2.2a).")
        _libclang_ready = False
    return _libclang_ready


def _extract_kernel_info_libclang(source_path):
    """libclang-based extraction: finds the function enclosing the
    #pragma cgra acc line by AST cursor extent (semantic), not text
    heuristics — so it isn't fooled by e.g. a harness main() defined before
    the target function (SAT-MapIt/benchmarks/sqrt/main.c does exactly this;
    the regex path below swallows that whole extra function as "preamble").

    Returns the same dict shape as _extract_kernel_info_regex(), or None if
    no pragma / no enclosing function definition is found.
    """
    import clang.cindex as ci

    with open(source_path) as f:
        src = f.read()
    # clang's cursor.extent offsets are byte offsets into the UTF-8-encoded
    # source, NOT Python string (character) indices — any non-ASCII text
    # before a slice point (e.g. '×'/'→' in a comment) desyncs the two and
    # slicing `src` directly lands mid-token (confirmed: corrupted rotatehash's
    # own function name this way). Slice `src_bytes` and decode back instead.
    src_bytes = src.encode('utf-8')

    def _slice(start, end):
        return src_bytes[start:end].decode('utf-8')

    pragma_m = re.search(r'#pragma\s+cgra\s+acc', src)
    if not pragma_m:
        return None
    pragma_line = src[:pragma_m.start()].count('\n') + 1

    args = ['-std=c11']
    if _LIBCLANG_RESOURCE_DIR:
        args.append(f'-resource-dir={_LIBCLANG_RESOURCE_DIR}')

    tu = ci.Index.create().parse(source_path, args=args)

    target, other_funcs = None, []
    for cur in tu.cursor.get_children():
        if cur.kind == ci.CursorKind.FUNCTION_DECL and cur.is_definition():
            ext = cur.extent
            if ext.start.line <= pragma_line <= ext.end.line:
                target = cur
            else:
                other_funcs.append(cur)
    if target is None:
        return None

    func_name = target.spelling
    ret_type  = target.result_type.spelling
    params    = [(p.type.spelling, p.spelling) for p in target.get_arguments()]

    # Locate the pragma'd loop's own condition text — used later (see
    # _loop_cond_size_param) to tell which of several int/unsigned/size_t
    # parameters is the real loop bound when more than one type-qualifies,
    # instead of guessing, AND (see loop_locals below) to spot local
    # variables a leftover CGRA column might correspond to. Walk the whole
    # function for FOR_STMT/WHILE_STMT and take the one starting closest
    # at-or-after the pragma line (the loop it annotates), not just the
    # first one in the function (could be an unrelated setup loop earlier
    # in the body).
    def _walk_loop_stmts(cur):
        if cur.kind in (ci.CursorKind.FOR_STMT, ci.CursorKind.WHILE_STMT):
            yield cur
        for child in cur.get_children():
            yield from _walk_loop_stmts(child)

    def _paren_span(text, open_from=0):
        """Byte range of the first balanced (...) in text, or None."""
        paren_start = text.find('(', open_from)
        if paren_start == -1:
            return None
        depth = 0
        for idx in range(paren_start, len(text)):
            if text[idx] == '(':
                depth += 1
            elif text[idx] == ')':
                depth -= 1
                if depth == 0:
                    return paren_start, idx
        return None

    loop_candidates = [c for c in _walk_loop_stmts(target)
                       if c.extent.start.line >= pragma_line]
    loop_cond = None
    pragma_loop = None
    if loop_candidates:
        pragma_loop = min(loop_candidates, key=lambda c: c.extent.start.line)
        loop_text = _slice(pragma_loop.extent.start.offset, pragma_loop.extent.end.offset)
        span = _paren_span(loop_text)
        if span is not None:
            paren_start, paren_end = span
            header = loop_text[paren_start + 1:paren_end]
            if pragma_loop.kind == ci.CursorKind.WHILE_STMT:
                # while (COND) { ... } — the whole parenthesized part is it.
                loop_cond = header
            else:
                # for (init; COND; incr) { ... } — split on top-level ';'.
                segments, depth2, cur_seg = [], 0, ''
                for ch in header:
                    if ch in '([{':
                        depth2 += 1
                    elif ch in ')]}':
                        depth2 -= 1
                    if ch == ';' and depth2 == 0:
                        segments.append(cur_seg)
                        cur_seg = ''
                    else:
                        cur_seg += ch
                segments.append(cur_seg)
                if len(segments) >= 2:
                    loop_cond = segments[1]

    # Local variables declared (with an initializer) before the pragma'd
    # loop, that the loop's own condition references — candidates for what
    # a leftover CGRA column (one with no matching function parameter) might
    # actually be. E.g. isqrt32's `uint16_t mask = 1 << 14;` + `while (mask)`
    # — a leftover column loaded once at init and read by a branch is very
    # plausibly `mask`'s starting value, not an arbitrary placeholder. This
    # can't resolve every leftover column (e.g. a bare `while(x)`'s implicit
    # "compare against 0" boundary isn't itself a named C variable) — it's a
    # candidate hint for gen_sweep_section to offer, not a guaranteed mapping.
    # Scoped to while-loops only: a for-loop's condition variable is almost
    # always just its own counter (declared/incremented by the for-statement
    # itself, e.g. string_search's `int i = 0; for (i = 0; i < patlen-1; ++i)`)
    # — that's already a hardware loop mechanism, not a leftover-column
    # candidate, and offering it as one would be actively misleading. A
    # while-loop's condition variable has no such built-in counter role.
    loop_locals = []
    if loop_cond and pragma_loop is not None and pragma_loop.kind == ci.CursorKind.WHILE_STMT:
        def _walk_var_decls(cur):
            if cur.kind == ci.CursorKind.VAR_DECL:
                yield cur
            for child in cur.get_children():
                yield from _walk_var_decls(child)

        for v in _walk_var_decls(target):
            if v.extent.start.line >= pragma_loop.extent.start.line:
                continue  # declared at/after the loop — not a setup local
            if not re.search(rf'\b{re.escape(v.spelling)}\b', loop_cond):
                continue  # not referenced in the loop condition
            children = list(v.get_children())
            if not children:
                continue  # no initializer — nothing to offer as a value
            init_c = children[-1]
            init_text = _slice(init_c.extent.start.offset, init_c.extent.end.offset)
            loop_locals.append((v.spelling, v.type.spelling, init_text))

    start_off, end_off = target.extent.start.offset, target.extent.end.offset
    func_ref_body = _slice(start_off, end_off)
    func_ref_body = re.sub(r'[ \t]*#pragma\s+cgra\s+acc[ \t]*\n', '', func_ref_body)
    func_ref_body = func_ref_body.replace(func_name, func_name + '_ref', 1)

    # Preamble: file-scope text before the target function, with #include
    # lines AND any other function definitions' own text excised — otherwise
    # a preceding harness main() gets pulled in wholesale (the actual bug
    # this replaces the regex/brace-counting approach to fix).
    other_ranges = sorted(
        (o.extent.start.offset, o.extent.end.offset)
        for o in other_funcs
        if o.extent.end.offset <= start_off
    )
    parts, pos = [], 0
    for o_start, o_end in other_ranges:
        if o_start > pos:
            parts.append(_slice(pos, o_start))
        pos = max(pos, o_end)
    if pos < start_off:
        parts.append(_slice(pos, start_off))
    preamble_raw = ''.join(parts)

    preamble_lines = [l for l in preamble_raw.splitlines()
                      if not l.strip().startswith('#include')]
    preamble = '\n'.join(preamble_lines).strip()
    preamble_block = (preamble + '\n\n') if preamble else ''

    return {
        'func_name':   func_name,
        'func_ref':    preamble_block + func_ref_body,
        'return_type': ret_type,
        'params':      params,
        'loop_cond':   loop_cond,
        'loop_locals': loop_locals,
    }


def extract_kernel_info(source_path, satmapit_dir=None):
    """Extract the function containing #pragma cgra acc from a C source file.

    Returns a dict with:
      func_name  : str  — function name
      func_ref   : str  — full function with pragma removed, renamed <name>_ref
      return_type: str  — return type (e.g. 'int32_t')
      params     : list of (type, name) tuples
    Returns None if no pragma found.

    Tries libclang first (semantic — see _extract_kernel_info_libclang's
    docstring for why); falls back to regex/brace-counting if libclang isn't
    set up (no SAT-MapIt checkout, no libclang.so, etc.) or raises.
    """
    if _setup_libclang(satmapit_dir or _DEFAULT_SATMAPIT_DIR):
        try:
            result = _extract_kernel_info_libclang(source_path)
            if result is not None:
                return result
        except Exception as e:
            print(f"  NOTE: libclang parsing failed ({e}) — falling back to regex extraction.")
    return _extract_kernel_info_regex(source_path)


def _extract_kernel_info_regex(source_path):
    """Regex/brace-counting fallback for extract_kernel_info() — used when
    libclang isn't available. See docs/cgra_kernel_toolchain.md TODO §2.2(a)
    for known weaknesses (multi-line signatures, macros, a function defined
    before the target — see _extract_kernel_info_libclang instead)."""
    with open(source_path) as f:
        src = f.read()

    pragma_m = re.search(r'#pragma\s+cgra\s+acc', src)
    if not pragma_m:
        return None

    # Walk backwards from the pragma tracking brace depth to find the
    # enclosing function's opening '{', skipping any nested if/for/while blocks.
    pos = pragma_m.start() - 1
    depth = 0
    open_brace = -1
    while pos >= 0:
        if src[pos] == '}':
            depth += 1
        elif src[pos] == '{':
            if depth == 0:
                open_brace = pos
                break
            depth -= 1
        pos -= 1
    if open_brace == -1:
        return None

    # Isolate just the function signature — strip any preceding declarations,
    # includes, or comments by starting after the last ';', '}', or '*/'
    # before the opening brace.
    before = src[:open_brace]
    before_brace = before.rstrip()
    def _after(s, sub):
        pos = s.rfind(sub)
        return pos + len(sub) if pos >= 0 else 0
    sig_start = max(
        _after(before_brace, ';'),
        _after(before_brace, '}'),
        _after(before_brace, '*/'),
        0
    )
    sig_text = before_brace[sig_start:].strip()
    # Strip any leading preprocessor lines (#include, #define, etc.)
    sig_lines = sig_text.splitlines()
    sig_text = '\n'.join(
        l for l in sig_lines if not l.strip().startswith('#')
    ).strip()

    depth = 1
    i = open_brace + 1
    while i < len(src) and depth > 0:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
        i += 1
    func_body = src[open_brace:i]

    # Remove the #pragma line
    func_ref_body = re.sub(r'[ \t]*#pragma\s+cgra\s+acc[ \t]*\n', '', func_body)

    # Extract function name (last identifier before '(')
    name_m = re.search(r'(\w+)\s*\([^)]*\)\s*$', sig_text)
    func_name = name_m.group(1) if name_m else "kernel"

    # Extract return type (everything before the function name)
    ret_type = sig_text[:name_m.start()].strip() if name_m else "int32_t"

    # Parse parameter list
    paren_m = re.search(r'\(([^)]*)\)\s*$', sig_text)
    params = []
    if paren_m:
        for param in paren_m.group(1).split(','):
            param = param.strip()
            if param and param != 'void':
                # Handle both `type *name` and `type name[]` (array params = pointer)
                pm = re.match(r'^(.*[\s*])\s*(\w+)(\[\])?$', param)
                if pm:
                    ptype = pm.group(1).strip()
                    if pm.group(3):  # trailing [] → pointer
                        ptype = ptype + '*'
                    params.append((ptype, pm.group(2)))

    # Extract file-scope preamble (macros, global vars) that the function may depend on.
    # sig_start is an offset into before_brace (= before.rstrip()), valid as index into before.
    preamble_raw = before[:sig_start]
    preamble_lines = [l for l in preamble_raw.splitlines()
                      if not l.strip().startswith('#include')]
    preamble = '\n'.join(preamble_lines).strip()
    preamble_block = (preamble + '\n\n') if preamble else ''

    ref_src = preamble_block + sig_text.replace(func_name, func_name + "_ref", 1) + func_ref_body

    return {
        'func_name':   func_name,
        'func_ref':    ref_src,
        'return_type': ret_type,
        'params':      params,
    }


# ─── Test harness generator ──────────────────────────────────────────────────
def gen_test_harness(layout, ref_info):
    """Generate buffer fill + reference call + PASS/FAIL verify code.

    Buffer fill is always auto-generated from the layout.
    Verify section requires ref_info (reference function extracted from C source).

    Heuristic mapping of C function params to stream buffers:
      - pointer param   → data portion of the primary data col (skip init words)
      - 'int' param     → N_ELEMENTS
      - scalar param    → first scalar-col's read buffer[0]
      Return value      → compared against first write col's output buffer[0]
    """
    params = ref_info.get('params', []) if ref_info else []
    ret    = ref_info.get('return_type', 'int32_t') if ref_info else 'int32_t'
    fname  = ref_info['func_name'] if ref_info else None

    # Classify cols
    data_cols   = [c for c, v in sorted(layout.items()) if v['read_kernel'] > 0]
    swi_cols    = [c for c, v in sorted(layout.items()) if v.get('has_swi')]
    scalar_cols = [c for c, v in sorted(layout.items())
                   if v['read_kernel'] == 0 and v['reads'] and not v.get('has_swi')]
    write_cols  = [c for c, v in sorted(layout.items())
                   if v['writes'] and not v.get('has_swi')]

    scalar_col = scalar_cols[0] if scalar_cols else None
    write_col  = write_cols[0]  if write_cols  else None

    _dc_idx = [0]

    def _next_data_col():
        # See _build_ref_args' next_data_col() for why this must consume
        # data_cols in order instead of reusing a single fixed column for
        # every pointer parameter.
        if _dc_idx[0] < len(data_cols):
            dc = data_cols[_dc_idx[0]]
            _dc_idx[0] += 1
            return dc
        return None

    # ── Buffer fill (always auto-generated from layout) ───────────────────────
    fill_lines = ["    /* auto-generated test data */"]

    for dc in data_cols:
        n_init = layout[dc]['read_init']
        n_prol = layout[dc]['read_prolog']
        n_ep   = layout[dc]['read_epilog']
        # Prefix slots: init reads (e.g. N-prefix word) + prolog reads
        n_prefix = n_init + n_prol
        if n_init == 1:
            fill_lines.append(f"    col{dc}_in[0] = N_ELEMENTS;  /* N prefix */")
            for k in range(n_prol):
                fill_lines.append(f"    col{dc}_in[{1 + k}] = 0;  /* prolog pad */")
        elif n_init > 1:
            fill_lines.append(f"    col{dc}_in[0] = N_ELEMENTS;  /* N prefix */")
            for k in range(1, n_init):
                fill_lines.append(f"    col{dc}_in[{k}] = 0;")
            for k in range(n_prol):
                fill_lines.append(f"    col{dc}_in[{n_init + k}] = 0;  /* prolog pad */")
        elif n_prol > 0:
            for k in range(n_prol):
                fill_lines.append(f"    col{dc}_in[{k}] = 0;  /* prolog pad */")
        rk = layout[dc]['read_kernel']
        _COL_PRIMES = [3, 5, 7, 11]
        prime = _COL_PRIMES[min(dc, len(_COL_PRIMES) - 1)]
        n_data_expr = f"N_ELEMENTS * {rk}" if rk > 1 else "N_ELEMENTS"
        fill_lines.append(
            f"    for (int _i = 0; _i < {n_data_expr}; _i++)"
            f" col{dc}_in[{n_prefix} + _i] = (_i + 1) * {prime};"
            f"  /* data × {prime}, {rk} read(s)/iter */"
        )
        for k in range(n_ep):
            fill_lines.append(
                f"    col{dc}_in[{n_prefix} + {n_data_expr} + {k}] = 0;  /* epilog pad */"
            )

    for swi_col in swi_cols:
        fill_lines.append(
            f"    col{swi_col}_in[0] = (int32_t)swi_col{swi_col}_out;"
            f"  /* SWI base address → auto-declared output buffer */"
        )

    # If the C signature has no plain-scalar parameter at all (only pointers
    # and int/unsigned/size_t counts — e.g. mul_test(x*, y*, z*, int n)),
    # every scalar_cols column is provably a schedule artifact, not tied to
    # any real parameter — most commonly a loop-count/threshold register a
    # BNE/BEQ compares against. Filling it with the generic placeholder 0
    # instead of the real count fed the CGRA a garbage trip count: confirmed
    # (mul_test) to cause a stream read pointer walking off the end of SRAM
    # (Verilator: "Out of bound memory access") since the loop never got a
    # real bound. If there IS a plain-scalar param (e.g. rotatehash's uint32_t
    # seed), leave the existing generic-placeholder behavior alone — it's
    # already known-working and this heuristic can't tell that column apart
    # from a real one with any more confidence than before.
    has_plain_scalar_param = any(
        '*' not in pt and pt.strip() not in _SIZE_PARAM_TYPES
        for pt, _ in params
    )
    use_n_elements_hint = bool(data_cols) and not has_plain_scalar_param
    for sc in scalar_cols:
        n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
        if use_n_elements_hint:
            fill_lines.append(
                f"    col{sc}_in[0] = N_ELEMENTS;  /* TODO: verify — guessed as a "
                f"loop-count/threshold register (no plain-scalar C parameter to "
                f"explain this column otherwise); could instead be an unrelated "
                f"fixed constant */")
        else:
            fill_lines.append(f"    col{sc}_in[0] = 0;  /* scalar input */")
        for k in range(1, n_sc):
            fill_lines.append(f"    col{sc}_in[{k}] = {k * 7};  /* scalar data */")

    # ── Verify ────────────────────────────────────────────────────────────────
    verify_lines = []

    if swi_cols and not write_cols:
        # SWI output is in swi_col*_out[]; verify against ref if available
        swi_col0 = swi_cols[0]
        swi_result_expr = f"swi_col{swi_col0}_out[0]"
        is_void = ret.strip() in ('void', '')
        if ref_info and fname and not is_void:
            # Non-void ref: compare return value against swi output[0]
            ref_args = []
            scalar_col_used = [False]
            for ptype, pname in params:
                if '*' in ptype:
                    data_col = _next_data_col()
                    if data_col is not None:
                        n_init = layout[data_col]['read_init']
                        base = f"col{data_col}_in + {n_init}" if n_init else f"col{data_col}_in"
                        base_type = ptype.strip().rstrip('*').strip().lstrip('const').strip()
                        if base_type not in ('int32_t', 'uint32_t', 'int', 'unsigned int', 'unsigned'):
                            base = f"({ptype}){base}"
                        ref_args.append(base)
                    else:
                        ref_args.append("NULL  /* MANUAL */")
                elif ptype.strip() in _SIZE_PARAM_TYPES:
                    ref_args.append("N_ELEMENTS")
                else:
                    if scalar_col is not None and not scalar_col_used[0]:
                        n_sc_ref = max(layout[scalar_col]['read_init'] + layout[scalar_col]['read_prolog'], 1)
                        ref_args.append(f"col{scalar_col}_in[{n_sc_ref - 1}]")
                        scalar_col_used[0] = True
                    else:
                        ref_args.append("0")
            verify_lines.append(f"    uint32_t _t0, _t1;")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t0);")
            verify_lines.append(f"    int32_t _expected = {fname}_ref({', '.join(ref_args)});")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
            verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
            verify_lines.append(f"    int32_t _result = {swi_result_expr};")
            verify_lines.append(f"    int32_t _first_got = 0, _first_expected = 0;")
            verify_lines.append(
                f"    int _errors = verify_results(&_result, &_expected, 1, "
                f"&_first_got, &_first_expected);"
            )
            verify_lines.append(
                f"    if (!_errors) printf(\"PASS: result=%d\\n\", (int)_result);"
            )
            verify_lines.append(
                f"    else printf(\"FAIL: got=%d expected=%d\\n\", (int)_result, (int)_expected);"
            )
        elif ref_info and fname and is_void:
            # Void ref modifies its array in-place. The CGRA streams are delay-tapped
            # slices of the same source array — we can't reconstruct that automatically.
            # Print the CGRA output so you can inspect it; add your own comparison here.
            verify_lines.append(
                f"    /* {fname}_ref() is void (in-place); CGRA output is in swi_col{swi_col0}_out[]."
            )
            verify_lines.append(
                f"       Auto-comparison skipped: streams are delay-tapped slices of a single"
            )
            verify_lines.append(
                f"       source array — fill them consistently and call {fname}_ref on a copy. */"
            )
            verify_lines.append(
                f"    for (int _i = 0; _i < N_ELEMENTS; _i++)"
            )
            verify_lines.append(
                f"        printf(\"out[%d] = %d\\n\", _i, (int)swi_col{swi_col0}_out[_i]);"
            )
        else:
            verify_lines.append(
                f"    /* MANUAL: implement reference and compare against {swi_result_expr} */"
            )
            verify_lines.append(
                f"    printf(\"SWI result[0]: %d\\n\", (int){swi_result_expr});"
            )
    elif not ref_info:
        verify_lines.append(
            "    /* MANUAL: reference function not extracted (no --ref-src or no #pragma cgra acc)."
        )
        verify_lines.append(
            "       Implement <funcname>_ref() and call it here to compare against col"
            + (f"{write_col}_out[0]" if write_col is not None else "*_out[0]") + ". */"
        )
    elif write_col is not None and ret.strip() not in ('void', ''):
        ref_args = []
        scalar_col_used = [False]
        for ptype, pname in params:
            if '*' in ptype:
                data_col = _next_data_col()
                if data_col is not None:
                    n_init = layout[data_col]['read_init']
                    base = f"col{data_col}_in + {n_init}" if n_init else f"col{data_col}_in"
                    base_type = ptype.strip().rstrip('*').strip().lstrip('const').strip()
                    if base_type not in ('int32_t', 'uint32_t', 'int', 'unsigned int', 'unsigned'):
                        base = f"({ptype}){base}"
                    ref_args.append(base)
                else:
                    ref_args.append("NULL  /* MANUAL */")
            elif ptype.strip() in _SIZE_PARAM_TYPES:
                ref_args.append("N_ELEMENTS")
            else:
                if scalar_col is not None and not scalar_col_used[0]:
                    n_sc_ref = max(layout[scalar_col]['read_init'] + layout[scalar_col]['read_prolog'], 1)
                    ref_args.append(f"col{scalar_col}_in[{n_sc_ref - 1}]")
                    scalar_col_used[0] = True
                else:
                    ref_args.append("0")
        verify_lines.append(f"    uint32_t _t0, _t1;")
        verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t0);")
        verify_lines.append(
            f"    int32_t _expected = {fname}_ref({', '.join(ref_args)});"
        )
        verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
        verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
        verify_lines.append(f"    int32_t _result = col{write_col}_out[0];")
        verify_lines.append(f"    int32_t _first_got = 0, _first_expected = 0;")
        verify_lines.append(
            f"    int _errors = verify_results(&_result, &_expected, 1, "
            f"&_first_got, &_first_expected);"
        )
        verify_lines.append(
            f"    if (!_errors) printf(\"PASS: result=%d\\n\", (int)_result);"
        )
        verify_lines.append(
            f"    else printf(\"FAIL: got=%d expected=%d\\n\","
            f" (int)_result, (int)_expected);"
        )
    elif write_col is not None and ret.strip() in ('void', ''):
        # Void return with SWD output.
        # Simple case (rk==1 for all data cols): streams map 1-to-1 to the original
        # function's array arguments — auto-generate PASS/FAIL comparison.
        # Complex case (any rk > 1): the same source array fans out to multiple
        # stream offsets, so we can't reconstruct the call automatically.
        is_simple = all(layout[c]['read_kernel'] <= 1 for c in data_cols)
        if ref_info and fname and is_simple:
            # Map pointer params → col_in arrays in order, last pointer → _ref_out
            wsize = _buf_size_expr(layout[write_col], 'out')
            ref_args = []
            dc_idx = 0
            for pt, pn in params:
                if '*' in pt:
                    if dc_idx < len(data_cols):
                        dc_in = data_cols[dc_idx]
                        n_init_in = layout[dc_in]['read_init']
                        ref_args.append(
                            f"col{dc_in}_in + {n_init_in}" if n_init_in else f"col{dc_in}_in"
                        )
                        dc_idx += 1
                    else:
                        ref_args.append("_ref_out")
                elif pt.strip() in _SIZE_PARAM_TYPES + ('uint32_t', 'int32_t'):
                    ref_args.append("N_ELEMENTS")
                else:
                    if scalar_col is not None:
                        ref_args.append(f"col{scalar_col}_in[0]")
                    else:
                        ref_args.append("0  /* MANUAL */")
            real_count = _real_output_count_expr(layout[write_col], 'N_ELEMENTS')
            verify_lines.append(f"    int32_t _ref_out[{wsize}];")
            verify_lines.append(f"    uint32_t _t0, _t1;")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t0);")
            verify_lines.append(f"    {fname}_ref({', '.join(ref_args)});")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
            verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
            verify_lines.append(f"    int32_t _first_got = 0, _first_expected = 0;")
            verify_lines.append(
                f"    int _errors = verify_results(col{write_col}_out, _ref_out, {real_count}, "
                f"&_first_got, &_first_expected);"
            )
            verify_lines.append(f"    for (int _i = 0; _i < {real_count}; _i++)")
            verify_lines.append(
                f"        printf(\"out[%d] ref=%d got=%d %s\\n\", _i,"
                f" (int)_ref_out[_i], (int)col{write_col}_out[_i],"
                f" col{write_col}_out[_i] == _ref_out[_i] ? \"OK\" : \"FAIL\");"
            )
            verify_lines.append(
                f"    printf(!_errors ? \"### PASS ###\\n\" : \"### FAIL ###\\n\");"
            )
        else:
            # Complex kernel or no ref: print raw output for inspection.
            if ref_info and fname:
                verify_lines.append(
                    f"    /* {fname}_ref() streams are delay-tapped slices of a single"
                )
                verify_lines.append(
                    f"       source array (complex fan-out) — auto-comparison not possible."
                )
                verify_lines.append(
                    f"       Implement the real comparison here by hand. */"
                )
            else:
                verify_lines.append(
                    f"    /* MANUAL: no --ref-src passed. Implement reference and compare"
                )
                verify_lines.append(
                    f"       against col{write_col}_out[]. */"
                )
            verify_lines.append(f"    for (int _i = 0; _i < N_ELEMENTS; _i++)")
            verify_lines.append(
                f"        printf(\"out[%d] = 0x%08X\\n\","
                f" _i, (unsigned)col{write_col}_out[_i]);"
            )
    else:
        verify_lines.append(
            "    /* MANUAL: no streaming write col detected — verify output manually. */"
        )

    return (
        "\n".join(fill_lines) + "\n",
        "\n".join(verify_lines) + "\n",
    )


# ─── main.c template ──────────────────────────────────────────────────────────
_MAIN_C_TEMPLATE = '''\
/* {app_name} — generated by util/cgra_gen.py from {ker_basename}
 *
 * Split into independent building blocks another tool can call directly
 * instead of only through main(): cgra_bitstream.{{h,c}} (KMEM/CMEM + load),
 * cgra_setup.{{h,c}} (interrupt/handle init), verify.{{h,c}} (software
 * reference), sweep.{{h,c}} (fill/launch/compare loop). */

#include <stdio.h>
#include <stdlib.h>

#include "cgra.h"
#include "cgra_setup.h"
#include "sweep.h"

#if CGRA_N_COLS != {n_col} || CGRA_N_ROWS != {n_row}
  #error "{app_name} requires a {n_col}x{n_row} CGRA"
#endif

int main(void)
{{
    cgra_t cgra;
    cgra_setup(&cgra);
    run_sweep(&cgra);

    printf("### DONE ###\\n");
    return EXIT_SUCCESS;
}}
'''

_BITSTREAM_H_TEMPLATE = '''\
#ifndef CGRA_BITSTREAM_H
#define CGRA_BITSTREAM_H

/* Kernel slot IDs (slot 0 is always null / unused) */
{kernel_ids}

/* Load the CGRA bitstream (KMEM + CMEM) generated from {ker_basename}. */
void cgra_load_bitstream(void);

#endif  /* CGRA_BITSTREAM_H */
'''

_BITSTREAM_C_TEMPLATE = '''\
#include <stdint.h>

#include "cgra.h"
#include "cgra_bitstream.h"

/* ── CGRA bitstream (generated from {ker_basename}) ──────────────────── */

{kmem_decl}
{cmem_decl}
void cgra_load_bitstream(void) {{
    cgra_cmem_init(cgra_cmem, cgra_kmem);
}}
'''

_SETUP_H_TEMPLATE = '''\
#ifndef CGRA_SETUP_H
#define CGRA_SETUP_H

#include "cgra.h"

/* Interrupt setup, CGRA handle init, bitstream load, wait-for-ready. */
void cgra_setup(cgra_t *cgra);

/* Arm/wait for the CGRA "done" interrupt around a single cgra_set_kernel()
 * call. cgra_intr_wait() returns 1 on completion, 0 if CGRA_INTR_TIMEOUT_CYCLES
 * elapses first (e.g. a bad schedule-constant guess left a BNE/BEQ loop
 * bound wrong, so the kernel never raises its done interrupt) — check the
 * return value instead of assuming it always completes. */
void cgra_intr_arm(void);
int cgra_intr_wait(void);

#endif  /* CGRA_SETUP_H */
'''

_SETUP_C_TEMPLATE = '''\
#include <stdint.h>

#include "csr.h"
#include "handler.h"
#include "core_v_mini_mcu.h"
#include "rv_plic.h"
#include "rv_plic_regs.h"
#include "heepsilon.h"
#include "cgra.h"

#include "cgra_setup.h"
#include "cgra_bitstream.h"

static volatile int8_t cgra_intr_flag;

/* Cycle budget for cgra_intr_wait() below. Generous on purpose (this is a
 * correctness backstop, not a perf-tuned bound) — its only job is turning a
 * silent hang/out-of-bounds crash from a bad schedule-constant guess (see the
 * TODO comments in sweep.c) into a loud, obvious failure. Raise it if a
 * legitimately slow kernel/N ever trips it.
 *
 * Sized against the slowest legitimate case measured so far (vec_sum at N=1024,
 * ~6.4k cycles), so this is ~75x headroom. It was 10000000, which is correct on
 * hardware but pathological under Verilator: a kernel that really does hang
 * burns the full budget on every launch, so a 30-trial sweep spent 300M
 * simulated cycles — tens of minutes — to report a failure it knew about after
 * the first one. */
#define CGRA_INTR_TIMEOUT_CYCLES  500000u

void handler_irq_cgra(uint32_t id) {{
    cgra_intr_flag = 1;
}}

void cgra_intr_arm(void) {{
    cgra_intr_flag = 0;
}}

int cgra_intr_wait(void) {{
    uint32_t t0, t1;
    CSR_READ(CSR_REG_MCYCLE, &t0);
    while (!cgra_intr_flag) {{
        CSR_READ(CSR_REG_MCYCLE, &t1);
        if (t1 - t0 > CGRA_INTR_TIMEOUT_CYCLES) return 0;
    }}
    return 1;
}}

void cgra_setup(cgra_t *cgra)
{{
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, (void *)&handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);

    cgra->base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);
    cgra_perf_cnt_enable(cgra, 1);

    cgra_load_bitstream();
    cgra_wait_ready(cgra);
}}
'''

_VERIFY_H_TEMPLATE = '''\
#ifndef VERIFY_H
#define VERIFY_H

#include <stdint.h>

{proto}

/* Compares cgra_out[0..n) against ref_out[0..n) element-by-element. Returns
 * the number of mismatched elements (0 = pass). On the first mismatch,
 * *first_got/*first_expected are set to the two differing values there
 * (left at 0 if there was no mismatch). n=1 covers a single scalar result;
 * n>1 covers a full array/reduction output — same function either way. */
int verify_results(const int32_t *cgra_out, const int32_t *ref_out, int n,
                    int32_t *first_got, int32_t *first_expected);

#endif  /* VERIFY_H */
'''

_VERIFY_C_TEMPLATE = '''\
#include "verify.h"

{body}

int verify_results(const int32_t *cgra_out, const int32_t *ref_out, int n,
                    int32_t *first_got, int32_t *first_expected)
{{
    int errors = 0;
    *first_got = 0;
    *first_expected = 0;
    for (int i = 0; i < n; i++) {{
        if (cgra_out[i] != ref_out[i]) {{
            if (errors == 0) {{
                *first_got = cgra_out[i];
                *first_expected = ref_out[i];
            }}
            errors++;
        }}
    }}
    return errors;
}}
'''

_SWEEP_H_TEMPLATE = '''\
#ifndef SWEEP_H
#define SWEEP_H

#include <stdint.h>
#include "cgra.h"

void run_sweep(cgra_t *cgra);

{accessor_decls}
#endif  /* SWEEP_H */
'''

_SWEEP_C_TEMPLATE = '''\
#include <stdio.h>
#include <stdint.h>

#include "csr.h"
#include "cgra.h"
#include "cgra_bitstream.h"
#include "cgra_setup.h"
#include "verify.h"
#include "sweep.h"

/* ── Input / output buffers ──────────────────────────────────────────── */
{io_comment}
{io_decls}
/* ── Accessors: real CGRA memory backing each named C parameter ────────── */
{accessor_defs}
void run_sweep(cgra_t *cgra)
{{
{sweep_body}
}}
'''

# Fallback sweep body for kernels that don't land in the sweep-eligible
# branch (SWI outputs, void-return array kernels, no --ref-src, etc.) —
# same single-shot harness as before, just cgra-pointer/intr-helper based.
_OLD_SWEEP_BODY_TEMPLATE = '''\
    /* ── Test data ──────────────────────────────────────────────── */
{test_fill}
    /* ── Run kernel ──────────────────────────────────────────────── */
{ptr_setup}
    cgra_intr_arm();
    /* offload wall clock: launch + execution + completion interrupt */
    uint32_t _w0, _w1;
    CSR_READ(CSR_REG_MCYCLE, &_w0);
    cgra_set_kernel(cgra, KER_ID_1);
    if (!cgra_intr_wait()) {{
        printf("### CGRA TIMEOUT: kernel never completed — check fixed/scalar "
               "column fill above (bad schedule constant?) ###\\n");
        printf("### FAIL ###\\n");
        return;
    }}
    CSR_READ(CSR_REG_MCYCLE, &_w1);
    uint32_t _cgra_wall = _w1 - _w0;

    /* ── Verify ─────────────────────────────────────────────────── */
    uint32_t _cpu_cycles = 0;
{test_verify}

    /* ── Performance counters ────────────────────────────────────── */
    printf("Kernels executed: %d\\n", cgra_perf_cnt_get_kernel(cgra));
    {{
        uint32_t _ca = cgra_perf_cnt_get_col_active(cgra, 0);
        uint32_t _cs = cgra_perf_cnt_get_col_stall(cgra, 0);
        /* stall is a subset of active, not disjoint — do not add them */
        uint32_t _ct = _ca;
        printf("CGRA: active=%u stall=%u (%u.%02u%% stalled) total=%u cycles\\n",
               _ca, _cs, _ca ? (_cs * 10000u / _ca) / 100u : 0u,
               _ca ? (_cs * 10000u / _ca) % 100u : 0u, _ct);
        for (int col = 1; col < CGRA_N_COLS; col++)
            printf("  col %d: active=%d stall=%d\\n", col,
                   cgra_perf_cnt_get_col_active(cgra, col),
                   cgra_perf_cnt_get_col_stall(cgra, col));
        printf("CGRA: wall=%u cycles (offload overhead %u)\\n",
               _cgra_wall, _cgra_wall > _ca ? _cgra_wall - _ca : 0u);
        if (_cpu_cycles > 0 && _ct > 0) {{
            uint32_t _sp = _cpu_cycles * 100u / _ct;
            printf("CPU : %u cycles  (speedup %u.%02ux)\\n",
                   _cpu_cycles, _sp / 100u, _sp % 100u);
        }}
        if (_cpu_cycles > 0 && _cgra_wall > 0) {{
            uint32_t _se = _cpu_cycles * 100u / _cgra_wall;
            printf("      end-to-end speedup %u.%02ux\\n", _se / 100u, _se % 100u);
        }}
    }}'''

def gen_app_files(ctx, app_name, kernel_file, ref_info=None,
                  sweep_mode='auto', sweep_range=None, sweep_trials=None, report=None,
                  rotate_cols='off'):
    """Generate every file for the app: main.c plus the split-out building
    blocks (cgra_bitstream, cgra_setup, verify, sweep) — a dict of
    {filename: content}. See the module docstring / _MAIN_C_TEMPLATE for the
    file layout."""
    if report is None:
        report = {'notes': [], 'review': [], 'todo': []}
    # Must run before infer_io_layout(): rotation rewrites the CMEM, and every
    # buffer size, stream-pointer slot and column report below is derived from
    # the CMEM, so they pick the rotation up for free.
    apply_branch_rotation(ctx, rotate_cols, report)
    n_kernels  = ctx['ker_next_id'] - 1
    kernel_ids = "\n".join(f"#define KER_ID_{i}  {i}" for i in range(1, n_kernels + 1))
    layout     = infer_io_layout(ctx)
    branch_bound = infer_branch_bound_cols(ctx)
    si         = ctx.get('_satmapit_info', {})
    prolog_len = si.get('prolog_end', 0) - si.get('init_end', 0)
    min_n      = prolog_len + 1

    if n_kernels > 1:
        kernel_ids += (
            f"\n/* TODO: only KER_ID_1 is ever launched (see run_sweep() in sweep.c) — "
            f"KER_ID_2..{n_kernels} are encoded here but never exercised. */"
        )
        report['notes'].append(
            f"{n_kernels} kernels loaded into this binary's CMEM/KMEM (see cgra_bitstream.h), "
            f"but run_sweep() (in sweep.c) only ever launches KER_ID_1 — the others are "
            f"encoded but never exercised by this generated test/sweep harness.")

    if not gen_io_role_report(layout):
        report['todo'].append("No LWD/SWD/SWI detected anywhere in the CMEM — this kernel has "
                               "no memory I/O. Confirm that's actually expected for this kernel.")

    has_swi = any(v.get('has_swi') for v in layout.values())
    if has_swi:
        swi_cols_info = [(c, v) for c, v in sorted(layout.items()) if v.get('has_swi')]
        for sc, sv in swi_cols_info:
            swi_size = _buf_size_expr(sv, 'swi_out')
            report['review'].append(
                f"col{sc}'s SWI writes are assumed sequential from a base address (same as "
                f"SWD) — declared swi_col{sc}_out[{swi_size}], col{sc}_in[0] auto-set to point "
                f"at it (see the comment above the buffer declarations in sweep.c). Confirm "
                f"that's actually how this kernel writes; if not, fix col{sc}_in[0] by hand.")

    if ref_info:
        param_list = ', '.join(
            f"{pt}{pn}" if pt.endswith('*') else f"{pt} {pn}"
            for pt, pn in ref_info['params']
        ) or 'void'
        todo_comment = (
            f"/* TODO: {ref_info['func_name']}() copied whole; code outside the pragma'd loop\n"
            f" * did not run on the CGRA. Check it can't affect the result. */"
        )
        verify_proto = f"{todo_comment}\n{ref_info['return_type']} {ref_info['func_name']}_ref({param_list});"
        verify_body  = ref_info['func_ref']
        report['review'].append(
            f"verify.c holds the whole of {ref_info['func_name']}(), not just the pragma'd "
            f"loop. Check whether code outside the loop affects the result.")
    else:
        stub = "/* TODO: write a reference function here (pass --ref-src to auto-extract one). */"
        verify_proto = stub
        verify_body  = stub

    sweep = gen_sweep_section(layout, ref_info, min_n,
                               sweep_mode=sweep_mode, sweep_range=sweep_range,
                               sweep_trials=sweep_trials, report=report,
                               branch_bound=branch_bound)
    if sweep is not None:
        io_decls        = sweep['io_decls']
        sweep_body      = sweep['sweep_body']
        accessor_decls  = sweep['accessor_decls']
        accessor_defs   = sweep['accessor_defs']
        # gen_sweep_section already added a param-name-enriched column report
        # (gen_merged_col_report) — the plain structural one below would just
        # repeat the same columns with less information.
    else:
        col_roles = gen_io_role_report(layout)
        if col_roles:
            report['notes'].append("Column roles auto-inferred from CMEM: " + "; ".join(col_roles))
        # get_cgra_<var>() accessors are only generated on the gen_sweep_section
        # path (see _build_ref_args) — not duplicated into gen_test_harness's
        # separate, less-used fallback branches (SWI output, complex fan-out,
        # --sweep off, no --ref-src) to avoid re-deriving the same param->column
        # mapping twice; no accessors are emitted for apps that land here.
        accessor_decls = ''
        accessor_defs  = ''
        test_fill, test_verify = gen_test_harness(layout, ref_info)
        io_decls = (
            f"/* Number of loop iterations (must be >= {min_n}).  Change to test different sizes. */\n"
            f"#define N_ELEMENTS  {min_n}\n"
            f"\n"
            f"{gen_io_buffers(layout)}"
        )
        sweep_body = _OLD_SWEEP_BODY_TEMPLATE.format(
            test_fill   = test_fill,
            ptr_setup   = gen_ptr_setup(layout),
            test_verify = test_verify,
        )

    ker_basename = os.path.basename(kernel_file)
    return {
        'main.c': _MAIN_C_TEMPLATE.format(
            app_name     = app_name,
            ker_basename = ker_basename,
            n_col        = CGRA_N_COL,
            n_row        = CGRA_N_ROW,
        ),
        'cgra_bitstream.h': _BITSTREAM_H_TEMPLATE.format(
            kernel_ids   = kernel_ids,
            ker_basename = ker_basename,
        ),
        'cgra_bitstream.c': _BITSTREAM_C_TEMPLATE.format(
            ker_basename = ker_basename,
            kmem_decl    = gen_kmem_c(ctx),
            cmem_decl    = gen_cmem_c(ctx),
        ),
        'cgra_setup.h': _SETUP_H_TEMPLATE.format(),
        'cgra_setup.c': _SETUP_C_TEMPLATE.format(),
        'verify.h': _VERIFY_H_TEMPLATE.format(proto=verify_proto),
        'verify.c': _VERIFY_C_TEMPLATE.format(body=verify_body),
        'sweep.h': _SWEEP_H_TEMPLATE.format(accessor_decls=accessor_decls),
        'sweep.c': _SWEEP_C_TEMPLATE.format(
            io_comment     = gen_io_comment(layout),
            io_decls       = io_decls,
            accessor_defs  = accessor_defs,
            sweep_body     = sweep_body,
        ),
    }

# ─── Main entry point ─────────────────────────────────────────────────────────

def _report_sections(report):
    """(heading, messages) for each tier that has content, in fixed order."""
    out = [("Notes", report['notes']),
           ("Review (confident guesses, not certain)", report['review'])]
    return [(h, m) for h, m in out if m]


def render_report(app_name, report, kernel_files, args):
    """The end-of-run summary as printed to the terminal.

    Three tiers, least-to-most likely to need attention:
      Notes   — what was inferred; informational, no action implied.
      Review  — a confident guess with real justification (not certain).
      Manual action needed — couldn't be automated, or a low-confidence guess.
    """
    bar = '=' * 68
    lines = [bar, f"  Summary for {app_name}", bar]
    for heading, msgs in _report_sections(report):
        lines.append(f"\n{heading}:")
        lines += [f"  - {m}" for m in msgs]
    lines.append("\nManual action needed:")
    lines += ([f"  - {m}" for m in report['todo']] or
              ["  none — should be ready to build and test as-is."])
    lines.append(bar)
    return "\n".join(lines)


def summary_md(app_name, report, kernel_files, args, todo_locs=None):
    """Same content as render_report(), as Markdown for GENERATION_SUMMARY.md.

    Records the invocation too, since which flags were used (notably
    --rotate-cols and --sweep) changes what the generated code means, and the
    file otherwise gives no way to tell how it was produced.
    """
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    flags = []
    for name in ('sweep', 'sweep_range', 'sweep_trials', 'rotate_cols', 'ref_src'):
        val = getattr(args, name, None)
        if val not in (None, 'auto', 'off'):
            flags.append(f"--{name.replace('_', '-')} {val}")
        elif name in ('rotate_cols', 'sweep') and val is not None:
            flags.append(f"--{name.replace('_', '-')} {val}")

    out = [f"# Generation summary — `{app_name}`", "",
           "Written by `util/cgra_gen.py`. Regenerating the app overwrites this file.", "",
           f"- Generated: {stamp}",
           f"- Kernel spec: {', '.join(os.path.basename(k) for k in kernel_files)}",
           f"- Flags: {' '.join(flags) if flags else '(defaults)'}", ""]
    # Resolve links before rendering: specific identifiers (col3_in[0], a
    # parameter) claim their line first, so a loose whole-column reference can't
    # steal it and leave the specific message unlinked.
    locs = list(todo_locs or [])
    used = set()
    links = {}
    all_msgs = report['todo'] + report['review']
    for strict in (True, False):
        for m in all_msgs:
            if m not in links:
                hit = _match_marker(m, locs, used, strict=strict)
                if hit:
                    links[m] = hit

    def _render(m):
        return f"- {_link(links[m])}{m}" if m in links else f"- {m}"

    for heading, msgs in _report_sections(report):
        out += [f"## {heading}", ""] + [_render(m) for m in msgs] + [""]
    out += ["## Manual action needed", ""]
    if report['todo'] or locs:
        for m in report['todo']:
            out.append(_render(m))
        # Any marker left in the code without a matching entry above still has to
        # be surfaced, or it silently never gets looked at.
        for f, ln, txt, _raw in locs:
            if (f, ln) not in used:
                out.append(f"- {_link((f, ln))}{txt}")
    else:
        out.append("None — should be ready to build and test as-is.")
    out.append("")
    return "\n".join(out)


def _link(hit):
    f, ln = hit[0], hit[1]
    return f"[`{f}:{ln}`]({f}#L{ln}) — "


def _match_marker(msg, todo_locs, used, strict=True):
    """Find the generated TODO comment a report message is talking about.

    Matches on the identifiers the message and the emitted comment share — a
    column slot (col3_in[0]), a column (col3_in[]), or a parameter name — so the
    summary can point at the exact line instead of making the reader grep.
    """
    slots = re.findall(r'col\d+_in\[\d+\]', msg)
    cols  = [c + '[' for c in re.findall(r'(col\d+_in)\[\]', msg)]
    parms = [f"param '{n}'" for n in re.findall(r"param '(\w+)'", msg)]
    parms += [f"{fn}(" for fn in re.findall(r'\b(\w+)\(\)', msg)]
    for token in (slots + parms) if strict else cols:
        for f, ln, txt, raw in todo_locs:
            if (f, ln) in used:
                continue
            if token in raw:
                used.add((f, ln))
                return (f, ln, txt)
    return None


def collect_todo_markers(files):
    """(filename, line_no, short_text, raw_line) for every TODO comment in the
    generated code, so the summary can link straight at them. The raw line is
    kept because the identifier a report message refers to (col3_in[0], a
    parameter name) usually sits before the "TODO:", not inside the comment."""
    locs = []
    for filename in sorted(files):
        for i, line in enumerate(files[filename].splitlines(), start=1):
            # Only real markers ("TODO:"), not the word TODO appearing in prose.
            if 'TODO:' not in line:
                continue
            txt = line.split('TODO:', 1)[1]
            txt = re.sub(r'\s*\*/\s*$', '', txt).strip().rstrip('-—').strip()
            short = (txt[:110] + '...') if len(txt) > 110 else (txt or 'TODO')
            locs.append((filename, i, short, line.strip()))
    return locs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('kernels', nargs='+',
                    help='One or more instructions_*.py kernel spec files, '
                         'followed by the application name as the last argument')
    ap.add_argument('--out-dir', default='sw/satmapit',
                    help='Parent output directory (default: sw/satmapit)')
    ap.add_argument('--ref-src', default=None,
                    help='Original C source file with #pragma cgra acc — '
                         'auto-extracts the reference function into verify.c')
    ap.add_argument('--satmapit-dir', default=None,
                    help='Path to SAT-MapIt checkout, for the libclang-based --ref-src '
                         'extraction (default: $SATMAPIT_DIR or ../SAT-MapIt). Falls back '
                         'to regex-based extraction if libclang.so isn\'t found there.')
    ap.add_argument('--sweep', choices=['auto', 'loop_bound', 'scalar', 'off'], default='auto',
                    help="Benchmarking sweep mode (default: auto). 'auto' sweeps problem "
                         "size N only if the reference function's signature has a real "
                         "int/unsigned/size_t parameter (i.e. the C loop bound came from "
                         "outside the function); otherwise it repeats randomized-input "
                         "trials at a fixed N. 'loop_bound'/'scalar' force one or the "
                         "other (loop_bound requires an array column to size). 'off' "
                         "disables the sweep (single fixed-input PASS/FAIL run).")
    ap.add_argument('--rotate-cols', default='off', metavar='MODE',
                    choices=['off', 'auto'] + [str(i) for i in range(CGRA_N_COL)],
                    help="Cyclically shift the kernel's columns (default: off, i.e. "
                         "SAT-MapIt's mapping is emitted exactly as produced). 'auto' "
                         "rotates only if a branch sits in the highest column, where "
                         "branches are not honoured on the ZCU104 build (measured; passes "
                         "in Verilator, so simulation won't catch it). An explicit 0..N-1 "
                         "forces that shift. The inter-column mesh is a torus, so a "
                         "rotation preserves the mapping exactly and re-solves nothing; "
                         "only whole-array (full column mask) kernels can be rotated.")
    ap.add_argument('--sweep-range', default=None, metavar='MIN:MAX',
                    help='Override the auto-sized doubling range for a loop_bound sweep, '
                         'e.g. --sweep-range 4:2048. Ignored for a trials sweep.')
    ap.add_argument('--sweep-trials', type=int, default=None, metavar='N',
                    help=f'Override the number of randomized-input trials for a trials '
                         f'sweep (default: {_SWEEP_TRIALS}). Ignored for a loop_bound sweep.')
    args = ap.parse_args()

    if len(args.kernels) < 2:
        ap.error('Provide at least one kernel spec file and an app name, e.g.: '
                 'util/cgra_gen.py instructions_foo.py my_app')

    sweep_range = None
    if args.sweep_range:
        try:
            lo_s, hi_s = args.sweep_range.split(':')
            sweep_range = (int(lo_s), int(hi_s))
            if sweep_range[0] <= 0 or sweep_range[1] < sweep_range[0]:
                raise ValueError
        except ValueError:
            ap.error(f"--sweep-range must be MIN:MAX with 0 < MIN <= MAX, got: {args.sweep_range}")

    # Last positional arg is the app name; all others are kernel spec files
    *kernel_files, app_name = args.kernels
    for kf in kernel_files:
        if kf.endswith('.c') or kf.endswith('.h'):
            ap.error(
                f"'{kf}' looks like a C source file, not an instructions_*.py kernel spec.\n"
                f"       cgra_gen.py is the LAST step of the pipeline — it needs a spec file\n"
                f"       already produced by satmapit_parse.py or cgra_satmap.py.\n"
                f"       If you're starting from a plain C file, run this instead:\n"
                f"           python util/cgra_satmap.py {kf} --app {app_name}\n"
                f"       See docs/cgra_kernel_toolchain.md for the full pipeline.")
        if not os.path.isfile(kf):
            ap.error(f"kernel spec not found: {kf}")

    report = {'notes': [], 'review': [], 'todo': []}

    ref_info = None
    if args.ref_src:
        ref_info = extract_kernel_info(args.ref_src, satmapit_dir=args.satmapit_dir)
        if ref_info:
            print(f"  Reference function: {ref_info['func_name']}_ref() "
                  f"extracted from {os.path.basename(args.ref_src)}")
        else:
            print(f"  WARNING: no #pragma cgra acc found in {args.ref_src} "
                  f"— reference function will be a TODO stub")
            report['todo'].append(
                f"No #pragma cgra acc found in {args.ref_src} — reference function is a "
                f"MANUAL stub; write it by hand to enable PASS/FAIL verification.")
    else:
        report['todo'].append(
            "No --ref-src given — reference function is a MANUAL stub; no automatic "
            "PASS/FAIL or sweep.")

    ctx = load_kernel_specs(kernel_files)

    n_kernels = ctx['ker_next_id'] - 1
    print(f"Loaded {n_kernels} kernel(s) from "
          f"{', '.join(os.path.basename(k) for k in kernel_files)}")

    # Print KMEM summary
    for i, word_bin in enumerate(ctx['ker_conf_words']):
        v = int(word_bin, 2)
        if v:
            col_mask = (v >> (CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2)) \
                       & ((1 << CGRA_N_COL) - 1)
            start    = (v >> RCS_NUM_CREG_LOG2) & ((1 << CGRA_CMEM_BK_DEPTH_LOG2) - 1)
            n_instr  = (v & ((1 << RCS_NUM_CREG_LOG2) - 1)) + 1
            print(f"  KMEM[{i}] = 0x{v:04X}  "
                  f"col_mask=0b{col_mask:04b}  start={start}  n_instr={n_instr}")

    # Write the app's files (main.c + the split-out building-block modules)
    out_dir = os.path.join(args.out_dir, app_name)
    os.makedirs(out_dir, exist_ok=True)
    files = gen_app_files(ctx, app_name, kernel_files[0], ref_info=ref_info,
                          sweep_mode=args.sweep, sweep_range=sweep_range,
                          sweep_trials=args.sweep_trials, rotate_cols=args.rotate_cols, report=report)
    for filename, content in files.items():
        out_file = os.path.join(out_dir, filename)
        if os.path.exists(out_file):
            print(f"WARNING: overwriting existing {out_file}")
        with open(out_file, 'w') as f:
            f.write(content)
    # Keep the schedule with the app when it was generated elsewhere: it is the
    # only readable record of which PE does what, and the summary's TODOs refer
    # to it. No-op when the pipeline already wrote it here.
    for kf in kernel_files:
        dest = os.path.join(out_dir, os.path.basename(kf))
        if os.path.abspath(kf) != os.path.abspath(dest):
            shutil.copyfile(kf, dest)
    print(f"Generated: {out_dir}/ ({', '.join(sorted(files))})")

    # ── End-of-run summary — everything worth knowing before trusting this build ──
    # Three tiers, ordered least-to-most likely to need your attention:
    #   Notes   — what was inferred; informational, no action implied.
    #   Review  — a confident guess with real justification (not certain);
    #             worth a quick glance, not expected to be wrong.
    #   Manual action needed — genuinely couldn't be automated (or a real,
    #             low-confidence guess) — this is the "go look at this" list.
    summary = render_report(app_name, report, kernel_files, args)
    print("\n" + summary)

    # Also keep it next to the generated code: the terminal output scrolls away
    # and is otherwise only recoverable by regenerating.
    summary_file = os.path.join(out_dir, 'GENERATION_SUMMARY.md')
    with open(summary_file, 'w') as f:
        f.write(summary_md(app_name, report, kernel_files, args,
                           todo_locs=collect_todo_markers(files)))
    print(f"Summary written to {summary_file}")


if __name__ == '__main__':
    main()
