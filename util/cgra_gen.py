#!/usr/bin/env python3
"""cgra_gen.py — generate sw/applications/<name>/main.c from a kernel spec.

The kernel spec is an instructions_*.py file (same format used by
cgra_bitstream_gen.py and the instructions_*.py files in
hw/vendor/esl_epfl_cgra/util/).

Run from the project root.

Usage:
    python util/cgra_gen.py <kernel.py> <app_name> [--out-dir sw/applications]

Example:
    python util/cgra_gen.py \\
        hw/vendor/esl_epfl_cgra/util/instructions_xorshifthash.py \\
        cgra_xorshifthash2

The generated main.c has:
  - Embedded KMEM and CMEM bitstream arrays (sparse format, non-NOP only)
  - Standard CGRA boilerplate (interrupts, PLIC, cgra_cmem_init)
  - TODO markers for test data, I/O buffer layout, and result verification
"""

import sys
import os
import re
import argparse
from math import ceil, log

# ─── CGRA configuration ───────────────────────────────────────────────────────
# Must match heepsilon_cfg.hjson (default 4×4).
CGRA_N_COL              = 4
CGRA_N_ROW              = 4
CGRA_MAX_COL            = 4
CGRA_CMEM_BK_DEPTH      = 128
CGRA_CMEM_BK_DEPTH_LOG2 = int(ceil(log(128, 2)))   # 7
CGRA_KMEM_DEPTH         = 16
RCS_NUM_CREG            = 32
RCS_NUM_CREG_LOG2       = int(ceil(log(32,  2)))    # 5
CGRA_KMEM_WIDTH         = CGRA_MAX_COL + CGRA_CMEM_BK_DEPTH_LOG2 + RCS_NUM_CREG_LOG2  # 16
CGRA_CMEM_TOT_DEPTH     = CGRA_N_ROW * CGRA_CMEM_BK_DEPTH                              # 512

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
    """Return cgra_set_read_ptr / cgra_set_write_ptr calls for each active col."""
    if not layout:
        return "    /* no stream pointers needed */"
    lines = []
    for col in sorted(layout):
        if layout[col]['reads']:
            lines.append(
                f"    cgra_set_read_ptr (&cgra, (uint32_t)col{col}_in,  {col});"
                f"  /* col {col} read */"
            )
        if layout[col]['writes'] and not layout[col].get('has_swi'):
            lines.append(
                f"    cgra_set_write_ptr(&cgra, (uint32_t)col{col}_out, {col});"
                f"  /* col {col} write */"
            )
    return "\n".join(lines)


# ─── C source parser ─────────────────────────────────────────────────────────
def extract_kernel_info(source_path):
    """Extract the function containing #pragma cgra acc from a C source file.

    Returns a dict with:
      func_name  : str  — function name
      func_ref   : str  — full function with pragma removed, renamed <name>_ref
      return_type: str  — return type (e.g. 'int32_t')
      params     : list of (type, name) tuples
    Returns None if no pragma found.
    """
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

    data_col   = data_cols[0]   if data_cols   else None
    scalar_col = scalar_cols[0] if scalar_cols else None
    write_col  = write_cols[0]  if write_cols  else None

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

    for sc in scalar_cols:
        n_sc = max(layout[sc]['read_init'] + layout[sc]['read_prolog'], 1)
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
                    if data_col is not None:
                        n_init = layout[data_col]['read_init']
                        base = f"col{data_col}_in + {n_init}" if n_init else f"col{data_col}_in"
                        base_type = ptype.strip().rstrip('*').strip().lstrip('const').strip()
                        if base_type not in ('int32_t', 'uint32_t', 'int', 'unsigned int', 'unsigned'):
                            base = f"({ptype}){base}"
                        ref_args.append(base)
                    else:
                        ref_args.append("NULL  /* MANUAL */")
                elif ptype.strip() in ('int', 'unsigned', 'size_t'):
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
            verify_lines.append(f"    {ret} _expected = {fname}_ref({', '.join(ref_args)});")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
            verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
            verify_lines.append(f"    {ret} _result   = {swi_result_expr};")
            verify_lines.append(
                f"    if (_result == _expected) printf(\"PASS: result=%d\\n\", (int)_result);"
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
                if data_col is not None:
                    n_init = layout[data_col]['read_init']
                    base = f"col{data_col}_in + {n_init}" if n_init else f"col{data_col}_in"
                    base_type = ptype.strip().rstrip('*').strip().lstrip('const').strip()
                    if base_type not in ('int32_t', 'uint32_t', 'int', 'unsigned int', 'unsigned'):
                        base = f"({ptype}){base}"
                    ref_args.append(base)
                else:
                    ref_args.append("NULL  /* MANUAL */")
            elif ptype.strip() in ('int', 'unsigned', 'size_t'):
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
            f"    {ret} _expected = {fname}_ref({', '.join(ref_args)});"
        )
        verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
        verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
        verify_lines.append(f"    {ret} _result   = col{write_col}_out[0];")
        verify_lines.append(
            f"    if (_result == _expected)"
            f" printf(\"PASS: result=%d\\n\", (int)_result);"
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
                elif pt.strip() in ('int', 'unsigned', 'size_t', 'uint32_t', 'int32_t'):
                    ref_args.append("N_ELEMENTS")
                else:
                    if scalar_col is not None:
                        ref_args.append(f"col{scalar_col}_in[0]")
                    else:
                        ref_args.append("0  /* MANUAL */")
            verify_lines.append(f"    int32_t _ref_out[{wsize}];")
            verify_lines.append(f"    uint32_t _t0, _t1;")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t0);")
            verify_lines.append(f"    {fname}_ref({', '.join(ref_args)});")
            verify_lines.append(f"    CSR_READ(CSR_REG_MCYCLE, &_t1);")
            verify_lines.append(f"    _cpu_cycles = _t1 - _t0;")
            verify_lines.append(f"    int _pass = 1;")
            verify_lines.append(f"    for (int _i = 0; _i < N_ELEMENTS; _i++)")
            verify_lines.append(
                f"        if (col{write_col}_out[_i] != _ref_out[_i]) _pass = 0;"
            )
            verify_lines.append(f"    for (int _i = 0; _i < N_ELEMENTS; _i++)")
            verify_lines.append(
                f"        printf(\"out[%d] ref=%d got=%d %s\\n\", _i,"
                f" (int)_ref_out[_i], (int)col{write_col}_out[_i],"
                f" col{write_col}_out[_i] == _ref_out[_i] ? \"OK\" : \"FAIL\");"
            )
            verify_lines.append(
                f"    printf(_pass ? \"### PASS ###\\n\" : \"### FAIL ###\\n\");"
            )
        else:
            # Complex kernel or no ref: print raw output for inspection.
            if ref_info and fname:
                verify_lines.append(
                    f"    /* {fname}_ref() streams are delay-tapped slices of a single"
                )
                verify_lines.append(
                    f"       source array — auto-comparison not possible. See verify.h"
                )
                verify_lines.append(
                    f"       in cgra_sha for a manually-written example. */"
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
/*
 * {app_name} — generated by util/cgra_gen.py from {ker_basename}
 *
 * TODO: describe what this kernel computes (one line).
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

#include "csr.h"
#include "hart.h"
#include "handler.h"
#include "core_v_mini_mcu.h"
#include "rv_plic.h"
#include "rv_plic_regs.h"
#include "heepsilon.h"
#include "cgra.h"

#if CGRA_N_COLS != {n_col} || CGRA_N_ROWS != {n_row}
  #error "{app_name} requires a {n_col}x{n_row} CGRA"
#endif

/* Kernel slot IDs (slot 0 is always null / unused) */
{kernel_ids}

/* ── CGRA bitstream (generated from {ker_basename}) ──────────────────── */

{kmem_decl}
{cmem_decl}
/* ── Input / output buffers ──────────────────────────────────────────── */
{io_comment}
/* Number of loop iterations (must be >= {min_n}).  Change to test different sizes. */
#define N_ELEMENTS  {min_n}

{io_buffers}
/* ── Interrupt ───────────────────────────────────────────────────────── */
static volatile int8_t cgra_intr_flag;

void handler_irq_cgra(uint32_t id) {{
    cgra_intr_flag = 1;
}}

/* ── Software reference (for verification) ───────────────────────────── */
{ref_func}

/* ── main ────────────────────────────────────────────────────────────── */
int main(void)
{{
    /* ── Interrupt setup ─────────────────────────────────────────── */
    plic_Init();
    plic_irq_set_priority(CGRA_INTR, 1);
    plic_irq_set_enabled(CGRA_INTR, kPlicToggleEnabled);
    plic_assign_external_irq_handler(CGRA_INTR, (void *)&handler_irq_cgra);
    CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);
    CSR_SET_BITS(CSR_REG_MIE, 1 << 11);

    /* ── CGRA handle ─────────────────────────────────────────────── */
    cgra_t cgra;
    cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);
    cgra_perf_cnt_enable(&cgra, 1);

    /* ── Load bitstream ──────────────────────────────────────────── */
    cgra_cmem_init(cgra_cmem, cgra_kmem);

    /* ── Test data ──────────────────────────────────────────────── */
{test_fill}
    /* ── Run kernel ──────────────────────────────────────────────── */
    cgra_wait_ready(&cgra);
{ptr_setup}
    cgra_intr_flag = 0;
    cgra_set_kernel(&cgra, KER_ID_1);
    while (!cgra_intr_flag) wait_for_interrupt();

    /* ── Verify ─────────────────────────────────────────────────── */
    uint32_t _cpu_cycles = 0;
{test_verify}

    /* ── Performance counters ────────────────────────────────────── */
    printf("Kernels executed: %d\\n", cgra_perf_cnt_get_kernel(&cgra));
    {{
        uint32_t _ca = cgra_perf_cnt_get_col_active(&cgra, 0);
        uint32_t _cs = cgra_perf_cnt_get_col_stall(&cgra, 0);
        uint32_t _ct = _ca + _cs;
        printf("CGRA: active=%u stall=%u total=%u cycles\\n", _ca, _cs, _ct);
        for (int col = 1; col < CGRA_N_COLS; col++)
            printf("  col %d: active=%d stall=%d\\n", col,
                   cgra_perf_cnt_get_col_active(&cgra, col),
                   cgra_perf_cnt_get_col_stall(&cgra, col));
        if (_cpu_cycles > 0 && _ct > 0) {{
            uint32_t _sp = _cpu_cycles * 100u / _ct;
            printf("CPU : %u cycles  (speedup %u.%02ux)\\n",
                   _cpu_cycles, _sp / 100u, _sp % 100u);
        }}
    }}

    printf("### DONE ###\\n");
    return EXIT_SUCCESS;
}}
'''

def gen_main_c(ctx, app_name, kernel_file, ref_info=None):
    n_kernels  = ctx['ker_next_id'] - 1
    kernel_ids = "\n".join(f"#define KER_ID_{i}  {i}" for i in range(1, n_kernels + 1))
    layout     = infer_io_layout(ctx)
    si         = ctx.get('_satmapit_info', {})
    prolog_len = si.get('prolog_end', 0) - si.get('init_end', 0)
    min_n      = prolog_len + 1

    has_swi = any(v.get('has_swi') for v in layout.values())
    if has_swi:
        swi_cols_info = [(c, v) for c, v in sorted(layout.items()) if v.get('has_swi')]
        for sc, sv in swi_cols_info:
            swi_size = _buf_size_expr(sv, 'swi_out')
            print(f"  ASSUMPTION: col{sc} uses SWI — declared swi_col{sc}_out[{swi_size}],"
                  f" col{sc}_in[0] auto-set to point at it.")
            print(f"              SWI writes sequentially from base address (same as SWD).")
            print(f"              If wrong: set col{sc}_in[0] manually in main.c.")

    if ref_info:
        ref_func = ref_info['func_ref']
    else:
        ref_func = (
            "/* MANUAL: reference function not auto-extracted\n"
            " *   (pass --ref-src <source.c> with a #pragma cgra acc function to get this).\n"
            " * Write your own reference here to enable PASS/FAIL comparison. */"
        )

    test_fill, test_verify = gen_test_harness(layout, ref_info)

    return _MAIN_C_TEMPLATE.format(
        app_name    = app_name,
        ker_basename= os.path.basename(kernel_file),
        n_col       = CGRA_N_COL,
        n_row       = CGRA_N_ROW,
        kernel_ids  = kernel_ids,
        kmem_decl   = gen_kmem_c(ctx),
        cmem_decl   = gen_cmem_c(ctx),
        io_comment  = gen_io_comment(layout),
        io_buffers  = gen_io_buffers(layout),
        ptr_setup   = gen_ptr_setup(layout),
        ref_func    = ref_func,
        min_n       = min_n,
        test_fill   = test_fill,
        test_verify = test_verify,
    )

# ─── Main entry point ─────────────────────────────────────────────────────────
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
                         'auto-extracts the reference function into main.c')
    args = ap.parse_args()

    if len(args.kernels) < 2:
        ap.error('Provide at least one kernel spec file and an app name, e.g.: '
                 'util/cgra_gen.py instructions_foo.py my_app')

    # Last positional arg is the app name; all others are kernel spec files
    *kernel_files, app_name = args.kernels
    for kf in kernel_files:
        if not os.path.isfile(kf):
            sys.exit(f"ERROR: kernel spec not found: {kf}")

    ref_info = None
    if args.ref_src:
        ref_info = extract_kernel_info(args.ref_src)
        if ref_info:
            print(f"  Reference function: {ref_info['func_name']}_ref() "
                  f"extracted from {os.path.basename(args.ref_src)}")
        else:
            print(f"  WARNING: no #pragma cgra acc found in {args.ref_src} "
                  f"— reference function will be a TODO stub")

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

    # Write main.c
    out_dir  = os.path.join(args.out_dir, app_name)
    out_file = os.path.join(out_dir, 'main.c')
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(out_file):
        print(f"WARNING: overwriting existing {out_file}")
    content = gen_main_c(ctx, app_name, kernel_files[0], ref_info=ref_info)
    with open(out_file, 'w') as f:
        f.write(content)
    print(f"Generated: {out_file}")


if __name__ == '__main__':
    main()
