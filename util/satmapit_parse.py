#!/usr/bin/env python3
"""satmapit_parse.py — for when you already ran SAT-MapIt by hand and have a
cgra-code-acc1 file. Translates it into instructions_*.py.

(If you're starting from a plain .c file instead, use util/cgra_satmap.py —
it runs clang/opt/the mapper for you and calls this automatically. If you
already have a reviewed instructions_*.py, use util/cgra_gen.py directly to
(re)generate main.c — that's the next/last step after this one.)

Default SAT-MapIt invocation (full 4×4 grid, -x 4 -y 4):
    ./cgralang -f my_kernel.c -x 4 -y 4

Full-grid mapping (SAT-MapIt x→HEEPsilon row, SAT-MapIt y→HEEPsilon column):
    SAT-MapIt col j (x-dir)  →  HEEPsilon row j
    SAT-MapIt row k (y-dir)  →  HEEPsilon column k
    SAT-MapIt time T          →  HEEPsilon PC T
    SAT-MapIt RCL/RCR         →  HEEPsilon RCT/RCB  (x-dir → vertical rows)
    SAT-MapIt RCT/RCB         →  HEEPsilon RCL/RCR  (y-dir → horizontal cols)

PE ordering in the acc1 instruction listing (row-major, x inner):
    PE index = sat_y * n_sat_x + sat_x
    sat_x (x-dir, 0..n_row-1)  →  HEEPsilon row
    sat_y (y-dir, 0..n_col-1)  →  HEEPsilon col

Automatic transforms applied to the schedule:
    1. SMUL → NOP           (address arithmetic, eliminated with LWD)
    2. LWI  → LWD           (streaming load replaces indexed load)
    3. Data-PE init LWD → NOP   (was loading a base address, not needed with LWD)
    4. Data-PE address SADD(Rn, RCx) → NOP  (address = base+offset, not needed)
    5. Prologue branches → NOP  (warn: kernel requires N >= prolog_len+1)
    6. Phi-node instructions → NOP  (ROUT retention handles loop-carried values)

Usage:
    python util/satmapit_parse.py <cgra-code-acc1> [--name my_kernel] \\
        [--n-row 4] [--n-col 4] [--out-dir sw/satmapit]

Example:
    python util/cgra_satmap.py my_kernel.c --n-row 4 --n-col 4
    # or directly:
    python util/satmapit_parse.py cgra-code-acc1 --name instructions_my_kernel
"""

import sys
import re
import os
import argparse

# ─── SAT-MapIt → HEEPsilon direction mapping ──────────────────────────────────
# Full-grid mode (-x N_ROW -y N_COL):
#   x-direction (SAT-MapIt columns) → HEEPsilon row direction
#   y-direction (SAT-MapIt rows)    → HEEPsilon column direction
_DIR_MAP_FULLGRID = {
    'RCL': 'RCT',   # SAT-MapIt x-left  (x-1) → HEEPsilon row above
    'RCR': 'RCB',   # SAT-MapIt x-right (x+1) → HEEPsilon row below
    'RCT': 'RCL',   # SAT-MapIt y-up    (y-1) → HEEPsilon col  left
    'RCB': 'RCR',   # SAT-MapIt y-down  (y+1) → HEEPsilon col  right
}

# Single-column mode (-x N_ROW -y 1):
#   In this mode SAT-MapIt emits RCT/RCB for the x-direction (inter-row)
#   connections, so no remapping is needed — identity.
_DIR_MAP_1ROW = {
    'RCT': 'RCT',
    'RCB': 'RCB',
    'RCL': 'RCL',
    'RCR': 'RCR',
}

# ─── Operation name mapping SAT-MapIt → HEEPsilon ────────────────────────────
_OP_MAP = {
    'NOP':   'NOP',
    'EXIT':  'EXIT',
    'SADD':  'SADD',
    'SSUB':  'SSUB',
    'SMUL':  'SMUL',
    'FXPMUL':'FXPMUL',
    'SLT':   'SLT',
    'SRT':   'SRT',
    'SRA':   'SRA',
    'SHL':   'SLT',
    'LSHR':  'SRT',
    'ASHR':  'SRA',
    'AND':   'LAND',
    'LAND':  'LAND',
    'OR':    'LOR',
    'LOR':   'LOR',
    'XOR':   'LXOR',
    'LXOR':  'LXOR',
    'NAND':  'LNAND',
    'LNAND': 'LNAND',
    'NOR':   'LNOR',
    'LNOR':  'LNOR',
    'XNOR':  'LXNOR',
    'LXNOR': 'LXNOR',
    'BEQ':   'BEQ',
    'BNE':   'BNE',
    'BLT':   'BLT',
    'BGE':   'BGE',
    'JUMP':  'JUMP',
    'LWD':   'LWD',
    'SWD':   'SWD',
    'LWI':   'LWI',
    'SWI':   'SWI',
}

_SRC_NAMES = {
    'ZERO', 'ROUT', 'SELF', 'RCL', 'RCR', 'RCT', 'RCB',
    'R0', 'R1', 'R2', 'R3', 'IMM',
}

_MESH_DIRS = {'RCL', 'RCR', 'RCT', 'RCB'}


# ─── Instruction parser ───────────────────────────────────────────────────────
def parse_instr(line):
    """Parse one SAT-MapIt instruction line.

    Returns dict with keys: op, dest, srcA, srcB, imm, raw_line
    """
    line = line.strip()
    result = {'op': 'NOP', 'dest': None, 'srcA': None, 'srcB': None,
              'imm': None, 'raw_line': line}

    tokens = [t.strip() for t in re.split(r'[\s,]+', line) if t.strip()]
    if not tokens or tokens[0].upper() == 'NOP':
        return result

    op = tokens[0].upper()
    result['op'] = op
    args = tokens[1:]

    if op == 'EXIT':
        return result

    if op in ('LWD', 'LWI'):
        result['dest'] = args[0] if args else 'ROUT'
        if len(args) > 1:
            try:
                result['imm'] = int(args[1])
            except ValueError:
                result['srcA'] = args[1]
        return result

    if op in ('SWD', 'SWI'):
        result['srcA'] = args[0] if args else 'ROUT'
        if len(args) > 1:
            try:
                result['imm'] = int(args[1])
            except ValueError:
                result['srcB'] = args[1]  # SWI: write-address source (mesh dir or reg)
        return result

    if op in ('BEQ', 'BNE', 'BLT', 'BGE', 'JUMP'):
        result['dest'] = '-'
        if len(args) >= 1:
            result['srcA'] = args[0]
        if len(args) >= 2:
            result['srcB'] = args[1]
        if len(args) >= 3:
            try:
                result['imm'] = int(args[2])
            except ValueError:
                result['imm'] = 0
        else:
            result['imm'] = 0
        return result

    # Standard: OP DEST, SRCA, SRCB_or_IMM
    if len(args) >= 1:
        result['dest'] = args[0]
    if len(args) >= 2:
        result['srcA'] = args[1]
    if len(args) >= 3:
        third = args[2]
        try:
            result['imm'] = int(third)
            result['srcB'] = 'IMM'
        except ValueError:
            result['srcB'] = third

    # 4th arg: flag source for BSFA/BZFA (SAT-MapIt: BSFA dest, mux_b, mux_a, flag_src)
    if op in ('BSFA', 'BZFA') and len(args) >= 4:
        result['flagA'] = args[3]

    return result


# ─── HEEPsilon instruction builder ───────────────────────────────────────────
def to_heepsilon(parsed, dir_map):
    """Convert a parsed SAT-MapIt instruction to a HEEPsilon tuple string."""
    op_sat = parsed['op']

    if op_sat == 'NOP':
        return 'rcs_nop_instr'

    op = _OP_MAP.get(op_sat, op_sat)

    def map_src(s):
        if s is None or s == '':
            return 'ZERO'
        su = s.upper()
        if su == 'ROUT':
            return 'SELF'
        return dir_map.get(su, su)

    def fmt(s):
        return f'"{s}"'

    if op == 'EXIT':
        return '["-", "-", "EXIT", "-", "-", "0"]'

    if op in ('LWD', 'LWI'):
        dest = parsed['dest'] or 'ROUT'
        dest_reg = '-' if dest == 'ROUT' else dest
        stride = str(parsed['imm']) if parsed['imm'] is not None else '4'
        return f'["-", "-", "{op}", {fmt(dest_reg)}, "-", "{stride}"]'

    if op in ('SWD', 'SWI'):
        srcA = map_src(parsed['srcA'])
        # SWI: srcB is the write-address source; SWD: srcB unused (stream pointer auto-increments)
        srcB = map_src(parsed['srcB']) if (op == 'SWI' and parsed.get('srcB')) else '-'
        stride = str(parsed['imm']) if parsed['imm'] is not None else '4'
        return f'[{fmt(srcA)}, {fmt(srcB)}, "{op}", "-", "-", "{stride}"]'

    if op in ('BEQ', 'BNE', 'BLT', 'BGE', 'JUMP'):
        srcA = map_src(parsed['srcA'])
        srcB = map_src(parsed['srcB']) if parsed['srcB'] else 'ZERO'
        imm  = str(parsed['imm']) if parsed['imm'] is not None else '0'
        return f'[{fmt(srcA)}, {fmt(srcB)}, "{op}", "-", "-", "{imm}"]'

    srcA = map_src(parsed['srcA'])
    if parsed['srcB'] == 'IMM' and parsed['imm'] is not None:
        srcB = 'IMM'
        imm  = str(parsed['imm'])
    elif parsed['srcB'] is not None:
        srcB = map_src(parsed['srcB'])
        imm  = '0'
    else:
        srcB = 'ZERO'
        imm  = '0'

    dest = parsed['dest'] or 'ROUT'
    dest_reg = '-' if dest == 'ROUT' else dest

    # BSFA/BZFA: SAT-MapIt arg order is (dest, mux_b_if_false, mux_a_if_true, flag_src).
    # HEEPsilon encoding: [mux_a, mux_b, op, dest, muxF, imm] where
    #   "if sign-flag → mux_a, else mux_b".
    # So HEEPsilon mux_a = SAT-MapIt args[2] (=srcB), mux_b = SAT-MapIt args[1] (=srcA).
    if op in ('BSFA', 'BZFA'):
        flagA_raw = (parsed.get('flagA') or '').upper()
        muxF = dir_map.get(flagA_raw, flagA_raw) if flagA_raw else '-'
        if muxF not in ('SELF', 'RCL', 'RCR', 'RCT', 'RCB'):
            muxF = '-'
        return f'[{fmt(srcB)}, {fmt(srcA)}, "{op}", {fmt(dest_reg)}, "{muxF}", "{imm}"]'

    return (f'[{fmt(srcA)}, {fmt(srcB)}, "{op}", {fmt(dest_reg)}, "-", "{imm}"]')


# ─── cgra-code-acc1 parser ────────────────────────────────────────────────────
def parse_acc1(filepath):
    """Parse a SAT-MapIt cgra-code-acc1 file.

    Returns a dict:
      n_pe        : int
      II          : int   (last/successful value in the file)
      init_len    : int
      prolog_len  : int
      kernel_len  : int
      epilog_len  : int
      fini_len    : int
      schedule    : {t: [instr_line, ...]}
      phi_nodes   : {(abs_t, pe_idx): True}
    """
    with open(filepath) as f:
        text = f.read()

    info = {
        'n_pe': None,
        'II': None,
        'init_len': 0, 'prolog_len': 0, 'kernel_len': 0,
        'epilog_len': 0, 'fini_len': 0,
        'schedule': {},
        'phi_nodes': {},
    }

    # Use the LAST II value (earlier attempts are UNSAT; last is the solution)
    for m in re.finditer(r'^II:\s*(\d+)', text, re.MULTILINE):
        info['II'] = int(m.group(1))

    for key in ('init_len', 'prolog_len', 'kernel_len', 'epilog_len', 'fini_len'):
        m = re.search(rf'{key}:\s*(\d+)', text)
        if m:
            info[key] = int(m.group(1))

    # Parse phi-node annotations: Id: N name: phi time: T pe: P Rout: R opA: X opB: Y
    kernel_start_abs = info['init_len'] + info['prolog_len']
    phi_pat = re.compile(
        r'^Id:\s*\d+\s+name:\s*phi\s+time:\s*(\d+)\s+pe:\s*(\d+)'
        r'(?:\s+Rout:\s*(\S+))?(?:\s+opA:\s*(\S+))?(?:\s+opB:\s*(\S+))?',
        re.MULTILINE | re.IGNORECASE
    )
    for m in phi_pat.finditer(text):
        kernel_t = int(m.group(1))
        pe       = int(m.group(2))
        rout     = (m.group(3) or 'ROUT').upper()
        opA      = (m.group(4) or '').upper()
        opB      = (m.group(5) or '').upper()
        abs_t    = kernel_start_abs + kernel_t
        info['phi_nodes'][(abs_t, pe)] = {'Rout': rout, 'opA': opA, 'opB': opB}

    # Extract T=N instruction sections.
    # The file has three passes of T= sections:
    #   1. Plain-text instructions  ← we want these
    #   2. Visual ASCII-art grid    ← filtered by |/_/- prefix
    #   3. Node-ID grid             ← filtered; but phi annotations follow the last T=N here
    # Strategy: keep the longest block per T value (visual blocks have 0 filtered lines;
    # phi annotations are filtered by excluding lines that start with 'Id:').
    lines = text.splitlines()

    def get_instr_lines(start_line, end_line):
        result = []
        for line in lines[start_line + 1: end_line]:
            s = line.strip()
            if not s:
                continue
            c = s[0]
            # Skip visual-grid separator lines, node-ID lines, and phi annotations
            if c in ('_', '-', '|', '*'):
                continue
            if s.startswith('Id:'):
                continue
            result.append(s)
        return result

    t_pattern = re.compile(r'^T\s*=\s*(\d+)\s*$', re.MULTILINE)
    t_positions = [(m.start(), int(m.group(1))) for m in t_pattern.finditer(text)]

    if not t_positions:
        print("WARNING: no T=N instruction sections found — check file format")
        return info

    t_line_indices = []
    for pos, t_val in t_positions:
        char_count = 0
        for i, line in enumerate(lines):
            char_count += len(line) + 1
            if char_count > pos:
                t_line_indices.append((i, t_val))
                break

    sched = {}
    for i, (start_line, t_val) in enumerate(t_line_indices):
        end_line = t_line_indices[i + 1][0] if i + 1 < len(t_line_indices) else len(lines)
        instr_lines = get_instr_lines(start_line, end_line)
        if len(instr_lines) > len(sched.get(t_val, [])):
            sched[t_val] = instr_lines

    info['schedule'] = sched
    if sched:
        info['n_pe'] = len(sched[min(sched.keys())])

    return info


# ─── Automatic schedule transforms ───────────────────────────────────────────
def apply_transforms(sched, info, n_row=4, n_col=4):
    """Apply LWI→LWD, SMUL→NOP, SWI→SWD, phi→NOP, address-SADD→NOP, prologue-branch→NOP.

    Returns (modified_sched, notes, lwi_pes) where:
      modified_sched  : {t: [raw_instr_string, ...]}  (NOP replaces removed ops)
      notes           : list of human-readable transform log lines
      lwi_pes         : set of pe_idx that had LWI in the kernel body
    """
    init_end     = info['init_len']
    prolog_end   = init_end + info['prolog_len']
    kernel_start = prolog_end
    kernel_end   = kernel_start + info['kernel_len']
    epilog_end   = kernel_end + info['epilog_len']
    phi_nodes    = info.get('phi_nodes', {})
    notes        = []
    n_pe         = info.get('n_pe', n_row * n_col)
    full_grid    = (n_pe == n_row * n_col and n_col > 1)
    n_sat_x      = n_row   # SAT-MapIt x  →  HEEPsilon row
    n_sat_y      = n_col   # SAT-MapIt y  →  HEEPsilon col

    # Identify data PEs: those with LWI in the kernel body
    lwi_pes = set()
    for t in range(kernel_start, kernel_end):
        for pe, raw in enumerate(sched.get(t, [])):
            if parse_instr(raw)['op'].upper() == 'LWI':
                lwi_pes.add(pe)

    # Identify SWI PEs and the PEs that compute the SWI write address (addr_pes).
    # SWI ROUT, srcB  →  srcB is the write-address mesh direction.
    # The adjacent PE in that direction typically has SADD(Rn, mesh) = base + offset.
    # We convert SWI → SWD (sequential write assumed) and NOP the address chain:
    #   1. The adjacent "address PE"'s init LWD (was loading write base address)
    #   2. The adjacent "address PE"'s address SADDs (Rn + mesh_dir)
    swi_pes      = set()
    swi_addr_pes = set()
    n_time_total = max(sched.keys()) + 1 if sched else 0
    for t in range(n_time_total):
        for pe, raw in enumerate(sched.get(t, [])):
            p = parse_instr(raw)
            if p['op'].upper() != 'SWI':
                continue
            swi_pes.add(pe)
            srcB = (p.get('srcB') or '').upper()
            if srcB not in _MESH_DIRS:
                continue  # ZERO or register: no adjacent PE to NOP
            sat_x = pe % n_sat_x
            sat_y = pe // n_sat_x
            if srcB == 'RCR':
                adj = (sat_y * n_sat_x + (sat_x + 1) % n_sat_x)
            elif srcB == 'RCL':
                adj = (sat_y * n_sat_x + (sat_x - 1) % n_sat_x)
            elif srcB == 'RCB':
                adj = (((sat_y + 1) % n_sat_y) * n_sat_x + sat_x)
            else:  # RCT
                adj = (((sat_y - 1) % n_sat_y) * n_sat_x + sat_x)
            # Confirm adj PE has address-computation SADD pattern (Rn + mesh)
            for t2 in range(n_time_total):
                raw2 = sched.get(t2, [None])[adj] if adj < len(sched.get(t2, [])) else 'NOP'
                p2 = parse_instr(raw2 or 'NOP')
                if p2['op'].upper() == 'SADD':
                    sA2 = (p2.get('srcA') or '').upper()
                    sB2 = (p2.get('srcB') or '').upper()
                    if sA2 in ('R0', 'R1', 'R2', 'R3') and sB2 in _MESH_DIRS:
                        swi_addr_pes.add(adj)
                        break

    # Identify accumulator PEs: PEs with SWD in the fini section.
    # After transforming SMUL→NOP and LWI→LWD the pipeline latency drops to 1
    # cycle so only ONE epilog accumulation step is needed (vs epilog_len steps
    # in the original multi-cycle SMUL schedule).  Keep the first SADD(RCx, SELF)
    # in the epilog for each accumulator PE and NOP the rest (Rule 8).
    n_time_total = max(sched.keys()) + 1 if sched else 0
    accumulator_pes = set()
    for t in range(epilog_end, n_time_total):
        for pe, raw in enumerate(sched.get(t, [])):
            if parse_instr(raw)['op'].upper() == 'SWD':
                accumulator_pes.add(pe)
    epilog_acc_seen = set()  # PEs that have emitted their first epilog accumulation

    # Build phi loop-carry redirect table.
    # A recurrence-carry phi has form: SADD Rn, opA, ZERO  (uses init source opA).
    # For loop iterations the phi should use opB (the loop-carried value, computed
    # in the previous iteration, e.g. the shifted mask from SRT).  When opB is a
    # named register different from opA, we:
    #   (a) redirect any init-section LWD that loads into opA → loads into opB instead,
    #   (b) redirect the phi instruction to use opB instead of opA.
    # phi_lwd_redirect: { pe → (old_reg, new_reg) }
    _NAMED_REGS = {'R0', 'R1', 'R2', 'R3'}
    phi_lwd_redirect = {}
    for (abs_t, pe), pmeta in phi_nodes.items():
        if not isinstance(pmeta, dict):
            continue
        opA = pmeta.get('opA', '').upper()
        opB = pmeta.get('opB', '').upper()
        if opA not in _NAMED_REGS or opB not in _NAMED_REGS or opA == opB:
            continue
        # Phi writes dest=Rout; check that the phi instruction's srcA matches opA
        instr_list = sched.get(abs_t, [])
        raw_phi = instr_list[pe] if pe < len(instr_list) else 'NOP'
        p_phi = parse_instr(raw_phi)
        if (p_phi.get('srcA') or '').upper() != opA:
            continue  # instruction srcA doesn't match opA — skip
        # opB must not already be loaded by a LWD into this PE in init (avoid double-redirect)
        already_has_opB_lwd = any(
            (parse_instr(sched.get(t2, [])[pe] if pe < len(sched.get(t2, [])) else 'NOP')
             .get('dest', '').upper() == opB
             and parse_instr(sched.get(t2, [])[pe] if pe < len(sched.get(t2, [])) else 'NOP')
             .get('op', '').upper() in ('LWD', 'LWI'))
            for t2 in range(init_end)
        )
        if already_has_opB_lwd:
            continue
        phi_lwd_redirect[pe] = (opA, opB)
        notes.append(f"PE{pe}: phi loop-carry redirect {opA}→{opB} "
                     f"(init LWD {opA}→{opB}, phi srcA {opA}→{opB})")

    # Classify phi nodes:
    #   register-init phi:  srcA is a register (R0-R3) or ZERO → ROUT retention handles it → NOP
    #   mesh-routing phi:   srcA is a mesh direction (RCL/RCR/RCT/RCB) → routes data between PEs → KEEP
    reg_init_phi_pes = set()   # PEs whose phi is register-init (should be NOP'd in kernel+epilog)
    for (abs_t, pe) in phi_nodes:
        instr_list = sched.get(abs_t, [])
        raw = instr_list[pe] if pe < len(instr_list) else 'NOP'
        p2 = parse_instr(raw)
        srcA = (p2.get('srcA') or '').upper()
        if srcA in ('R0', 'R1', 'R2', 'R3', 'ZERO', 'SELF', 'ROUT'):
            reg_init_phi_pes.add(pe)

    modified = {}
    for t, instr_list in sorted(sched.items()):
        mod_list = []
        for pe, raw in enumerate(instr_list):
            p = parse_instr(raw)
            op = p['op'].upper()

            # Rule 0: SWI → SWD (sequential write assumed; SMUL was already removed so
            # the dynamic address chain is broken — convert to stream write instead).
            if op == 'SWI':
                srcA_str = p.get('srcA') or 'ROUT'
                new_raw = f"SWD {srcA_str}"
                mod_list.append(new_raw)
                notes.append(f"T={t} PE{pe}: SWI → SWD (sequential write assumed;"
                             f" use cgra_set_write_ptr; address chain will be NOPd)")
                continue

            # Rule 0c: Phi loop-carry redirect.
            # Redirect init-section LWDs from old_reg → new_reg for PEs with a
            # redirected phi so that the loop-carry register (opB) holds the init
            # value instead of the no-longer-needed init register (opA).
            if pe in phi_lwd_redirect and t < init_end and op in ('LWD', 'LWI'):
                old_reg, new_reg = phi_lwd_redirect[pe]
                dest_str = (p.get('dest') or 'ROUT').upper()
                if dest_str == old_reg:
                    new_raw = re.sub(r'\b' + old_reg + r'\b', new_reg, raw)
                    mod_list.append(new_raw)
                    notes.append(f"T={t} PE{pe}: init LWD redirected {old_reg}→{new_reg} "
                                 f"(phi loop-carry)")
                    continue

            # Rule 0b: NOP the write-address computation chain that fed the SWI.
            # These PEs only existed to compute &W[i] = base + i*4; after SWI→SWD
            # the base address comes from cgra_set_write_ptr instead.
            if pe in swi_addr_pes:
                if t < init_end and op in ('LWD', 'LWI'):
                    mod_list.append('NOP')
                    notes.append(f"T={t} PE{pe}: init LWD removed (was SWI write-address base load)")
                    continue
                if op == 'SADD':
                    srcA = (p.get('srcA') or '').upper()
                    srcB_tmp = (p.get('srcB') or '').upper()
                    if srcA in ('R0', 'R1', 'R2', 'R3') and srcB_tmp in _MESH_DIRS:
                        mod_list.append('NOP')
                        notes.append(f"T={t} PE{pe}: address SADD removed (SWI write-address chain)")
                        continue

            # Rule 1a: register-init phi in kernel body → NOP only when safe.
            # "Safe" = the phi does NOT write to a named register (R0-R3).
            # If it writes to ROUT (dest=='-' or dest=='ROUT'), ROUT retention
            # handles the loop-carried value so NOP is fine.
            # If it writes to R0-R3, it is carrying a recurrence into a register
            # that a downstream instruction reads by name — NOP would leave the
            # register at its initial value (0) and break the loop body.
            if (t, pe) in phi_nodes:
                srcA = (p.get('srcA') or '').upper()
                dest = (p.get('dest') or 'ROUT').upper()
                writes_named_reg = dest in ('R0', 'R1', 'R2', 'R3')
                if srcA in ('R0', 'R1', 'R2', 'R3', 'ZERO', 'SELF', 'ROUT') and not writes_named_reg:
                    mod_list.append('NOP')
                    notes.append(f"T={t} PE{pe}: register-init phi '{raw.strip()}' → NOP")
                    continue
                elif writes_named_reg:
                    # Recurrence carrier: must keep — NOP would freeze R0-R3 at 0.
                    # If there's a loop-carry redirect for this PE, replace srcA with opB.
                    if pe in phi_lwd_redirect:
                        old_reg, new_reg = phi_lwd_redirect[pe]
                        if srcA == old_reg:
                            raw = re.sub(r'\b' + old_reg + r'\b', new_reg, raw, count=1)
                            notes.append(f"T={t} PE{pe}: recurrence-carry phi redirected "
                                         f"srcA {old_reg}→{new_reg} (loop-carry): '{raw.strip()}'")
                        else:
                            notes.append(f"T={t} PE{pe}: recurrence-carry phi '{raw.strip()}' "
                                         f"kept (writes {dest})")
                    else:
                        notes.append(f"T={t} PE{pe}: recurrence-carry phi '{raw.strip()}' kept (writes {dest})")
                else:
                    # mesh-routing phi: srcA is RCL/RCR/RCT/RCB — must keep for inter-PE routing
                    notes.append(f"T={t} PE{pe}: mesh-routing phi '{raw.strip()}' kept")
                # fall through to normal processing (re-parse redirected raw)

            # Rule 1b: epilog version of register-init phi → also NOP
            # Same SADD(Rn, ZERO) pattern for the same PE appears in epilog too
            if (pe in reg_init_phi_pes and kernel_end <= t < epilog_end
                    and op == 'SADD'):
                srcA = (p.get('srcA') or '').upper()
                srcB = (p.get('srcB') or '').upper()
                imm  = p.get('imm') or 0
                if (srcA in ('R0', 'R1', 'R2', 'R3', 'ZERO')
                        and srcB in ('ZERO', 'IMM') and imm == 0):
                    mod_list.append('NOP')
                    notes.append(f"T={t} PE{pe}: epilog phi-reset '{raw.strip()}' → NOP")
                    continue

            # Rule 2: SMUL → NOP only when it is address arithmetic (PE is in LWI
            # chain AND this specific SMUL multiplies by a bare immediate, e.g.
            # `SMUL ROUT, RCT, 4` -- SAT-MapIt always encodes an address-stride
            # multiply this way). SMUL for data computation (e.g. squaring in
            # isqrt, or a genuine two-array product) has a named register/mesh
            # source as srcB (e.g. `SMUL ROUT, RCT, ROUT`) and must be kept.
            #
            # The `p.get('srcB') == 'IMM'` check matters because SAT-MapIt's
            # scheduler can legitimately pack a real data-computation SMUL onto
            # the very same physical PE that also loads data for an unrelated
            # LWI at a different time-slot -- checking "is this PE ever
            # involved in any LWI" alone (the old condition) deletes that real
            # multiply as collateral damage. Confirmed via a two-array kernel
            # (z[i] = x[i] * y[i]): the real product's SMUL landed on the same
            # PE as the y[i] load and was wrongly NOP'd, so the CGRA silently
            # emitted a passthrough of the loaded array instead of the product.
            #
            # Known residual gap: a source-level multiply by a literal constant
            # (e.g. `z[i] = x[i] * 3`) on a PE that also happens to be in
            # lwi_pes would still be misidentified as address arithmetic here,
            # since it has the same immediate-operand shape. Not yet seen in
            # any tested kernel; would need a dataflow-based distinction
            # (trace whether this specific SMUL's result actually feeds an
            # LWI's address) to close fully.
            if op == 'SMUL' and pe in lwi_pes and p.get('srcB') == 'IMM':
                mod_list.append('NOP')
                notes.append(f"T={t} PE{pe}: SMUL removed (address arithmetic)")
                continue

            # Rule 3: data PE + init section + LWD/LWI → NOP (was base-address load)
            # Exception: keep init LWDs that write to a named register (R0-R3) —
            # those are counter/scalar initialisations (e.g. loop upper bound into R0),
            # not address-chain base loads.
            if pe in lwi_pes and t < init_end and op in ('LWD', 'LWI'):
                dest = (p.get('dest') or 'ROUT').upper()
                if dest not in ('R0', 'R1', 'R2', 'R3'):
                    mod_list.append('NOP')
                    notes.append(f"T={t} PE{pe}: init LWD removed (was base-address load)")
                    continue

            # Rule 4: data PE + SADD(Rn, mesh-dir) → NOP (address computation)
            if pe in lwi_pes and op == 'SADD':
                srcA = (p.get('srcA') or '').upper()
                srcB = (p.get('srcB') or '').upper()
                if srcA in ('R0', 'R1', 'R2', 'R3') and srcB in _MESH_DIRS:
                    mod_list.append('NOP')
                    notes.append(f"T={t} PE{pe}: address SADD removed ('{raw.strip()}')")
                    continue

            # Rule 5: LWI → LWD
            if op == 'LWI':
                new_raw = re.sub(r'\bLWI\b', 'LWD', raw, count=1)
                mod_list.append(new_raw)
                notes.append(f"T={t} PE{pe}: LWI → LWD")
                continue

            # Rule 6: prologue branch → NOP
            if init_end <= t < prolog_end and op in ('BEQ', 'BNE', 'BLT', 'BGE'):
                mod_list.append('NOP')
                notes.append(f"T={t} PE{pe}: prologue {op} removed"
                              f" (warn: N must be >= {info['prolog_len'] + 1})")
                continue

            # Rule 8: epilog accumulation — only keep the first mesh-SADD per
            # accumulator PE; NOP subsequent ones (SMUL→NOP + LWI→LWD reduces
            # pipeline depth to 1 cycle, so only 1 drain step is needed).
            if (pe in accumulator_pes and kernel_end <= t < epilog_end and op == 'SADD'):
                # SADD is commutative and the mesh operand can land in either
                # field: clang emits `add <load>, <acc-phi>` (mesh dir in srcA),
                # while a shader's `acc += data[i]` emits `add <acc-phi>, <load>`
                # (mesh dir in srcB). Checking srcA alone silently skipped this
                # rule for the latter, leaving an extra epilog accumulation that
                # corrupts the result. Found in spike-1a; see SPIKE1A_NOTES.md.
                srcA = (p.get('srcA') or '').upper()
                srcB = (p.get('srcB') or '').upper()
                if srcA in _MESH_DIRS or srcB in _MESH_DIRS:
                    if pe in epilog_acc_seen:
                        mod_list.append('NOP')
                        notes.append(f"T={t} PE{pe}: extra epilog accumulation → NOP"
                                     " (streaming LWD needs only 1 drain step)")
                        continue
                    epilog_acc_seen.add(pe)

            mod_list.append(raw)

        modified[t] = mod_list

    return modified, notes, lwi_pes


# ─── instructions_*.py writer ─────────────────────────────────────────────────
def gen_instructions_py(info, kernel_name, n_row=4, n_col=4):
    """Generate an instructions_*.py file with auto-transforms applied."""
    sched_orig = info['schedule']
    n_pe       = info['n_pe']
    II         = info['II']
    n_time     = max(sched_orig.keys()) + 1 if sched_orig else 0

    one_row_mode   = (n_pe == n_row)
    full_grid_mode = (n_pe == n_row * n_col and n_col > 1)

    if one_row_mode:
        dir_map     = _DIR_MAP_1ROW
        n_sat_x     = n_row
        config_note = (
            f"# n_pe={n_pe} = n_row → single-column mode\n"
            f"# SAT-MapIt col j → HEEPsilon row j, col 0 only\n"
        )
    elif full_grid_mode:
        dir_map     = _DIR_MAP_FULLGRID
        n_sat_x     = n_row
        config_note = (
            f"# n_pe={n_pe} = {n_row}×{n_col} → full-grid mode\n"
            f"# SAT-MapIt x-col j (x-dir) → HEEPsilon row j\n"
            f"# SAT-MapIt y-row k (y-dir) → HEEPsilon col k\n"
        )
    else:
        dir_map     = {}
        n_sat_x     = n_row
        config_note = (
            f"# WARNING: n_pe={n_pe} doesn't match n_row={n_row} "
            f"or n_row×n_col={n_row*n_col} — review manually\n"
        )

    # Apply automatic transforms
    sched, notes, lwi_pes = apply_transforms(sched_orig, info, n_row=n_row, n_col=n_col)

    # Determine active (non-NOP) cols after transforms
    active_cols = set()
    for t, instr_list in sched.items():
        for pe, raw in enumerate(instr_list):
            if parse_instr(raw)['op'].upper() != 'NOP':
                if full_grid_mode:
                    heep_col = pe // n_sat_x
                else:
                    heep_col = 0
                active_cols.add(heep_col)

    if active_cols:
        max_col = max(active_cols)
        heep_n_col = max_col + 1  # contiguous: 0..max_col
    else:
        heep_n_col = 1

    lines = []
    lines.append("#")
    lines.append(f"# {kernel_name} — auto-generated by util/satmapit_parse.py")
    lines.append("#")
    lines.append(f"# SAT-MapIt stats: II={II}, "
                 f"init={info['init_len']}, prolog={info['prolog_len']}, "
                 f"kernel={info['kernel_len']}, epilog={info['epilog_len']}, "
                 f"fini={info['fini_len']}, total={n_time} time steps")
    lines.append("#")
    lines.append(config_note)

    if notes:
        lines.append("# Auto-transforms applied:")
        for n in notes:
            lines.append(f"#   {n}")
        lines.append("#")

    if info['prolog_len'] > 0:
        lines.append(f"# WARNING: prologue branches removed — kernel requires N >= "
                     f"{info['prolog_len'] + 1} (loop count).")
        lines.append("#")

    lines.append("# Stream layout (per column — set cgra_set_read_ptr / cgra_set_write_ptr):")
    lines.append("#   Review which columns load from stream (have LWD) and what they expect.")
    if lwi_pes:
        if full_grid_mode:
            data_cols = sorted({pe // n_sat_x for pe in lwi_pes})
        else:
            data_cols = [0]
        lines.append(f"#   Data-loading col(s) after transform: {data_cols}")
    lines.append("#   NOTE: each active column needs its own read pointer set before kernel launch.")
    lines.append("#")

    lines.append(f"ker_col_needed = {heep_n_col}  # cols 0..{heep_n_col-1} active")
    lines.append(f"ker_num_instr  = {n_time}  # T=0..{n_time-1}")
    lines.append("")
    lines.append("ker_conf_words[ker_next_id] = (")
    lines.append("    get_bin(int(pow(2, ker_col_needed)) - 1, CGRA_N_COL) +")
    lines.append("    get_bin(ker_start_add, CGRA_CMEM_BK_DEPTH_LOG2) +")
    lines.append("    get_bin(ker_num_instr - 1, RCS_NUM_CREG_LOG2)")
    lines.append(")")
    lines.append("start_add     = ker_start_add")
    lines.append("ker_start_add = ker_start_add + ker_num_instr * ker_col_needed")
    lines.append("ker_next_id   = ker_next_id + 1")
    lines.append("")

    def emit_pe_block(pe, heep_row, heep_col, label):
        imem_base = f"start_add + {heep_col}*ker_num_instr" if heep_col > 0 else "start_add"

        has_non_nop = any(
            parse_instr(sched[t][pe])['op'].upper() != 'NOP'
            for t in sorted(sched.keys()) if pe < len(sched[t])
        )
        if not has_non_nop:
            return  # skip fully-NOP PEs

        lines.append(f"# {label} → row={heep_row} col={heep_col}")

        # Annotate kernel/prolog/epilog boundaries
        kstart = info['init_len'] + info['prolog_len']
        kend   = kstart + info['kernel_len']

        for t in sorted(sched.keys()):
            instr_list = sched[t]
            raw = instr_list[pe] if pe < len(instr_list) else 'NOP'
            p   = parse_instr(raw)

            if p['op'].upper() == 'NOP':
                continue  # omit NOP lines — sparse assignment default is NOP

            section = ('init' if t < info['init_len']
                       else 'prolog' if t < kstart
                       else 'kernel' if t < kend
                       else 'epilog' if t < kstart + info['kernel_len'] + info['epilog_len']
                       else 'fini')

            orig_raw = sched_orig[t][pe] if pe < len(sched_orig.get(t, [])) else raw
            comment = f"  # T={t} [{section}]"
            if orig_raw.strip() != raw.strip():
                comment += f"  (was: {orig_raw.strip()})"

            heep_tuple = to_heepsilon(p, dir_map)
            lines.append(
                f"rcs_instructions[{heep_row}][{imem_base} + {t}]"
                f" = {heep_tuple}{comment}")

        lines.append("")

    for pe in range(n_pe):
        if one_row_mode:
            heep_row = pe
            heep_col = 0
            label    = f"PE {pe} (col {pe})"
        elif full_grid_mode:
            sat_col  = pe % n_sat_x
            sat_row  = pe // n_sat_x
            heep_row = sat_col
            heep_col = sat_row
            label    = f"PE {pe} (sat_x={sat_col} sat_y={sat_row})"
        else:
            lines.append(f"# PE {pe}: unknown layout")
            continue

        emit_pe_block(pe, heep_row, heep_col, label)

    # Emit section boundary metadata so cgra_gen.py can compute buffer sizes
    _init_end   = info['init_len']
    _prolog_end = _init_end + info['prolog_len']
    _kernel_end = _prolog_end + info['kernel_len']
    _epilog_end = _kernel_end + info['epilog_len']
    lines.append("# ── Section metadata (read by cgra_gen.py to auto-size buffers) ──")
    lines.append("_satmapit_info = {")
    lines.append(f"    'init_end':    {_init_end},")
    lines.append(f"    'prolog_end':  {_prolog_end},")
    lines.append(f"    'kernel_end':  {_kernel_end},")
    lines.append(f"    'epilog_end':  {_epilog_end},")
    lines.append(f"    'n_time':      {n_time},")
    lines.append(f"    'ii':          {II},")
    lines.append("}")

    return "\n".join(lines) + "\n"


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('acc1_file', help='SAT-MapIt cgra-code-acc1 output file')
    ap.add_argument('--name',    default=None,
                    help='Output filename stem (default: instructions_<basename>)')
    ap.add_argument('--n-pe',   type=int, default=None,
                    help='Override inferred PE count')
    ap.add_argument('--n-row',  type=int, default=4, help='HEEPsilon N_ROW (default 4)')
    ap.add_argument('--n-col',  type=int, default=4, help='HEEPsilon N_COL (default 4)')
    ap.add_argument('--out-dir', default='sw/satmapit', help='Output directory')
    ap.add_argument('--stdout', action='store_true', help='Print to stdout')
    ap.add_argument('--pipeline', action='store_true',
                    help='Suppress footer (used when called from cgra_satmap.py)')
    args = ap.parse_args()

    if not os.path.isfile(args.acc1_file):
        sys.exit(f"ERROR: file not found: {args.acc1_file}")

    info = parse_acc1(args.acc1_file)
    if args.n_pe:
        info['n_pe'] = args.n_pe

    n_pe = info['n_pe']
    if n_pe is None:
        sys.exit("ERROR: could not infer PE count; use --n-pe")

    n_time = max(info['schedule'].keys()) + 1 if info['schedule'] else 0
    print(f"SAT-MapIt: n_pe={n_pe}, II={info['II']}, "
          f"init={info['init_len']}, prolog={info['prolog_len']}, "
          f"kernel={info['kernel_len']}, epilog={info['epilog_len']}, "
          f"fini={info['fini_len']}, total={n_time}")
    print(f"Phi nodes: {len(info['phi_nodes'])} — {list(info['phi_nodes'].keys())}")

    if n_pe == args.n_row:
        print(f"→ single-column mode: col j → row j")
    elif n_pe == args.n_row * args.n_col:
        print(f"→ full-grid mode ({args.n_row}×{args.n_col})")
    else:
        print(f"→ WARNING: n_pe={n_pe} unexpected for {args.n_row}×{args.n_col}")

    base = os.path.splitext(os.path.basename(args.acc1_file))[0]
    kernel_name = args.name or f"instructions_{base}"

    content = gen_instructions_py(info, kernel_name, n_row=args.n_row, n_col=args.n_col)

    if args.stdout:
        print(content)
        return

    out_file = os.path.join(args.out_dir, kernel_name + '.py')
    os.makedirs(args.out_dir, exist_ok=True)
    if os.path.exists(out_file):
        print(f"WARNING: overwriting {out_file}")
    with open(out_file, 'w') as f:
        f.write(content)
    print(f"Generated: {out_file}")
    if not args.pipeline:
        print(f"Next: python util/cgra_gen.py {out_file} <app_name>")


if __name__ == '__main__':
    main()
