# OpenEdgeCGRA ISA Reference

This document was reverse-engineered from the RTL sources in
`hw/vendor/esl_epfl_cgra/hw/rtl/`. There is no separate ISA specification;
this file is the authoritative written reference for this project.

Key source files: `cgra_pkg.sv`, `datapath.sv`, `alu.sv`, `cgra_rcs.sv`,
`cgra_controller.sv`, `data_bus_handler.sv`, `program_counter.sv`.

---

## Architecture overview

The CGRA is a 2-D array of **N_ROWS × N_COL** Reconfigurable Cells (RCs).
Default configuration (HEEPsilon): **4 rows × 4 columns**.

- All columns execute in **lock-step SIMD**: every column in an active row
  executes the same instruction word at the same PC.
- Each column has its own **read pointer** and **write pointer** (set by the
  CPU before launching a kernel) and its own register file.
- Columns are connected in a **torus mesh**: each cell can read the result of
  its left, right, top, or bottom neighbour. Neighbour connections are
  **registered** — the value seen at cycle N+1 is the result produced by the
  neighbour at cycle N.
- Each row has its own **CMEM bank** (128 words). Rows within the same kernel
  run different instruction streams (pipeline stages).

---

## Instruction word format

Every instruction is a 32-bit word:

```
 31      28 27      24 23    19 18  17 16 15  13 12           0
 ┌─────────┬─────────┬────────┬──────┬──┬──────┬──────────────┐
 │  mux_a  │  mux_b  │ opcode │reg_sel│rw│flag_s│   imm_val   │
 └─────────┴─────────┴────────┴──────┴──┴──────┴──────────────┘
   4 bits    4 bits    5 bits   2 bits 1b  3 bits   13 bits
```

| Field     | Bits    | Description |
|-----------|---------|-------------|
| `mux_a`   | [31:28] | Source for operand A (see mux encoding below) |
| `mux_b`   | [27:24] | Source for operand B (see mux encoding below) |
| `opcode`  | [23:19] | ALU / control opcode |
| `reg_sel` | [18:17] | Destination register index (0–3) |
| `reg_we`  | [16]    | 1 = write ALU result to `reg[reg_sel]` |
| `flag_s`  | [15:13] | Flag mux select (used by BSFA/BZFA) |
| `imm_val` | [12:0]  | Signed 13-bit immediate (stride for LWD/SWD, coefficient for SMUL, branch offset for JUMP) |

An all-zero word is a **NOP** (opcode 0 = `CGRA_ALU_NOP`): no operation, no
register write, `rcs_res_reg` is not updated.

---

## Mux source encoding (`mux_a` / `mux_b`)

| Value | Source |
|-------|--------|
| 0     | Constant zero |
| 1     | `own_res` — own result registered from the previous cycle (see timing notes) |
| 2     | Left neighbour result (registered, 1-cycle latency) |
| 3     | Right neighbour result (registered, 1-cycle latency) |
| 4     | Top neighbour result (registered, 1-cycle latency) |
| 5     | Bottom neighbour result (registered, 1-cycle latency) |
| 6     | `reg[0]` |
| 7     | `reg[1]` |
| 8     | `reg[2]` |
| 9     | `reg[3]` |
| 10    | `imm_val` sign-extended to 32 bits |

---

## Register file

Each RC has 4 × 32-bit registers (`reg[0]`–`reg[3]`). A register is written
when `reg_we = 1` at the end of a cycle where `pc_e = 1` (no stall).

For **LWD**: the register write happens when `data_rvalid` arrives (may be 1–2
cycles after the LWD instruction), independent of `pc_e`. The PC does not
advance until the load is complete.

---

## Opcode table

### Arithmetic

| Opcode | Mnemonic  | Binary  | Operation |
|--------|-----------|---------|-----------|
| 0      | NOP       | 00000   | No operation |
| 1      | SADD      | 00001   | `result = mux_a + mux_b` |
| 2      | SSUB      | 00010   | `result = mux_a - mux_b` |
| 3      | SMUL      | 00011   | `result = mux_a × mux_b` (signed, lower 32 bits); **3-cycle stall** |
| 4      | FXPMUL    | 00100   | Fixed-point multiply (Q16.15 format); **3-cycle stall** |
| 26     | SABS      | 11010   | `result = (mux_a < 0) ? -mux_a : mux_a` (signed absolute value) |

### Shift

| Opcode | Mnemonic | Binary | Operation |
|--------|----------|--------|-----------|
| 5      | SLL      | 00101  | `result = mux_a << mux_b[4:0]` (logical left shift) |
| 6      | SRL      | 00110  | `result = mux_a >> mux_b[4:0]` (logical right shift) |
| 7      | SRA      | 00111  | `result = mux_a >>> mux_b[4:0]` (arithmetic right shift) |

### Bitwise logic

| Opcode | Mnemonic | Binary | Operation |
|--------|----------|--------|-----------|
| 8      | LAND     | 01000  | `result = mux_a & mux_b` |
| 9      | LOR      | 01001  | `result = mux_a \| mux_b` |
| 10     | LXOR     | 01010  | `result = mux_a ^ mux_b` |
| 11     | LNAND    | 01011  | `result = ~(mux_a & mux_b)` |
| 12     | LNOR     | 01100  | `result = ~(mux_a \| mux_b)` |
| 13     | LNXOR    | 01101  | `result = ~(mux_a ^ mux_b)` |

### Control flow (branches and conditional select)

All branch instructions compute `mux_a - mux_b` internally for the comparison.
`result` output is the 1-bit comparison result zero-extended to 32 bits.

| Opcode | Mnemonic | Binary | Branch taken when |
|--------|----------|--------|-------------------|
| 14     | BSFA     | 01110  | Conditional select: if sign-flag, output = mux_a, else mux_b |
| 15     | BZFA     | 01111  | Conditional select: if zero-flag, output = mux_a, else mux_b |
| 16     | BEQ      | 10000  | `mux_a == mux_b` |
| 17     | BNE      | 10001  | `mux_a != mux_b` |
| 18     | BLT      | 10010  | `mux_a < mux_b` (signed) |
| 19     | BGE      | 10011  | `mux_a >= mux_b` (signed) |
| 20     | JUMP     | 10100  | Always; target = `(mux_a + mux_b)[4:0]` (computed address) |

**Branch target:** For BEQ/BNE/BLT/BGE, the branch target is always **PC=0**.
When the branch is taken, the instruction at PC=0 executes, then execution
continues from PC=1. This creates a natural loop structure where PC=0 is a
per-iteration prologue (e.g., a load) and PC=1 is the loop body start.

For **JUMP**, the target address is the lower 5 bits of `mux_a + mux_b`.
After executing the instruction at the target, PC advances to target+1.

### Memory access

Memory access instructions use the column's hardware address counters
(`rd_data_cnt_col`, `wr_data_cnt_col`) which are initialised from the
read/write pointers set by the CPU before kernel launch. The `imm_val` field
is a **signed byte stride** added to the counter on each access.

| Opcode | Mnemonic | Binary | Description |
|--------|----------|--------|-------------|
| 21     | LWD      | 10101  | Load word from `rd_data_cnt`, advance counter by `imm_val` bytes. Value stored in `reg[reg_sel]` when data arrives. |
| 22     | SWD      | 10110  | Store `mux_a` to `wr_data_cnt`, advance counter by `imm_val` bytes. |
| 23     | LWI      | 10111  | Load word from address in `mux_a` (indirect). Value stored in `reg[reg_sel]`. |
| 24     | SWI      | 11000  | Store `mux_b` to address in `mux_a` (indirect). |
| 25     | EXIT     | 11001  | Terminate this column. When all rows of an active column have fired EXIT, the column is marked done. When all active columns are done, the CGRA interrupt fires. |

For **LWD with stride=0** (`imm_val=0`): the address counter does not advance,
so the same address is read every time.

---

## KMEM word format

The Kernel Memory (KMEM) holds one word per kernel ID. KMEM depth = 16
(kernel IDs 0–15). Kernel 0 is reserved (NULL).

```
 15      12 11       5 4         0
 ┌─────────┬──────────┬──────────┐
 │col_mask │start_addr│ n_instr-1│
 └─────────┴──────────┴──────────┘
   4 bits     7 bits     5 bits
```

| Field       | Bits   | Description |
|-------------|--------|-------------|
| `col_mask`  | [15:12] | One-hot active column mask (e.g. `0b1111` = all 4 columns) |
| `start_addr`| [11:5]  | CMEM bank start address (0–127) |
| `n_instr-1` | [4:0]   | Number of instructions minus 1 (max 31 instructions) |

Example: `0xF00E` = all 4 columns, start=0, 15 instructions (14+1).

---

## CMEM layout

The CGRA context memory is a flat array of `CGRA_CMEM_BK_DEPTH * CGRA_N_ROWS`
= 512 words. Row `r` maps to CMEM indices `[r*128 .. r*128+127]`. The
instruction for row `r` at PC `p` (relative to kernel start) is at index
`r*128 + start_addr + p`.

All columns in a row execute the same instruction at each PC — the CGRA is
**column-SIMD** (not row-SIMD). Each row provides a different pipeline stage.

---

## Timing notes and known gotchas

### `own_res` latency

`own_res` (mux source 1) is `rcs_res_reg`, a registered copy of the cell's
last result. It is updated only when `pc_e = 1` (no stall in effect).

- At PC=N+1, `own_res` = result produced at PC=N (one-cycle latency, normal).
- **After SMUL/FXPMUL stall**: at the cycle the stall releases (`pc_e=1`),
  `rcs_res_reg` is written with the multiply result AND the PC advances
  simultaneously. In practice, reading `own_res` at the immediately following
  instruction has been observed to be unreliable — the value seen may be the
  result from before the stall, not the multiply result.
  **Workaround**: use `reg_we=1` on the SMUL instruction to save the result
  into `reg[N]`, then use `reg[N]` (mux sources 6–9) in the following
  instruction instead of `own_res` (mux source 1).

### SMUL / FXPMUL stall

SMUL and FXPMUL produce a stall of 3 cycles total (the multiply cycle + 2
wait cycles). This is implemented via a 2-bit down-counter (`dp_stall_reg`)
in `cgra_controller.sv`. The PC does not advance during the stall.
No instruction should be placed at PC+1 with the expectation of consuming the
multiply result via `own_res` — see workaround above.

### LWD timing

LWD goes through a 2-phase AHB bus transaction (address phase + data phase).
The PC stalls while the data phase is in progress (`data_stall = 1`). The
register file write (`reg[reg_sel] ← loaded_value`) fires on `data_rvalid`,
independent of `pc_e`. The next instruction executes after the load is
complete, so `reg[reg_sel]` is always valid by PC+1.

### Neighbour result latency

Neighbour results (`left`/`right`/`top`/`bottom`, mux 2–5) go through a
register in `cgra_rcs.sv` (`rcs_mesh_res`). The value available at cycle N is
the result the neighbour produced at cycle N−1.

### INT_MIN and SABS

`SABS(INT32_MIN)` returns `INT32_MIN` (not representable as positive int32).
This matches the C expression `(int32_t)(-INT32_MIN)` and is consistent
across hardware and software.

---

## Instruction encoding examples

```
NOP  (all zero):                0x00000000

SADD(zero, zero) → reg[1]:     0x000B0000
  mux_a=0  mux_b=0  op=1(SADD)  reg_sel=1  reg_we=1  imm=0

LWD stride=4 → reg[0]:         0x00A90004
  mux_a=0  mux_b=0  op=21(LWD)  reg_sel=0  reg_we=1  imm=4

SMUL(reg[0], imm=2) → reg[2]:  0x6A1A0002
  mux_a=6(reg[0])  mux_b=10(imm)  op=3(SMUL)  reg_sel=2  reg_we=1  imm=2

SADD(reg[2], reg[1]) → reg[1]: 0x870B0000
  mux_a=8(reg[2])  mux_b=7(reg[1])  op=1(SADD)  reg_sel=1  reg_we=1  imm=0

SWD(reg[1], stride=16):         0x70B00010
  mux_a=7(reg[1])  mux_b=0  op=22(SWD)  reg_sel=0  reg_we=0  imm=16

BGE(reg[0], zero):              0xA0980000  (loops back to PC=0)
  mux_a=10(imm→0)  mux_b=0(zero)  op=19(BGE)  reg_sel=0  reg_we=0  imm=0
  (imm=0 is also the loop counter decrement target — use SSUB on counter first)

SABS(reg[0]) → reg[1]:         0x60D30000
  mux_a=6(reg[0])  mux_b=0  op=26(SABS)  reg_sel=1  reg_we=1  imm=0

EXIT:                            0x00C80000
  op=25(EXIT), all other fields zero
```

---

## Typical loop pattern

```
PC  0   LWD  stride=S → reg[0]    ← runs on first entry AND on every loop-back
PC  1   <computation using reg[0]>
PC  2   ...
PC  N   SWD  <result>, stride=S
PC N+1  SSUB(counter, imm=1) → reg[3]   ← decrement loop counter
PC N+2  BGE(reg[3], zero)               ← branch back to PC=0 if counter >= 0
PC N+3  EXIT
```

The CPU sets up the kernel as:
```c
cgra_input[0] = (int32_t)&data_array[0];  // read pointer
cgra_input[1] = array_length - 1;         // initial loop counter
cgra_set_read_ptr (&cgra, (uint32_t)cgra_input, col);
cgra_set_write_ptr(&cgra, (uint32_t)output_array, col);
cgra_set_kernel(&cgra, KERNEL_ID);
```
