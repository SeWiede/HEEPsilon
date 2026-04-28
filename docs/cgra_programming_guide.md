# OpenEdgeCGRA Programming Guide

This document describes the OpenEdgeCGRA architecture as integrated in HEEPsilon, how kernels are assembled, and what kinds of code it can accelerate.

---

## Architecture Overview

The CGRA is a **4×4 grid** of Reconfigurable Cells (RCs). Rows are numbered 0–3, columns 0–3.

```
        Col 0    Col 1    Col 2    Col 3
Row 0  [RC0,0]  [RC0,1]  [RC0,2]  [RC0,3]
Row 1  [RC1,0]  [RC1,1]  [RC1,2]  [RC1,3]
Row 2  [RC2,0]  [RC2,1]  [RC2,2]  [RC2,3]
Row 3  [RC3,0]  [RC3,1]  [RC3,2]  [RC3,3]
```

Each RC has:
- Its own ALU
- 4 local registers: R0, R1, R2, R3
- A neighbour routing network (RCL/RCR/RCT/RCB)

**All rows in a column share one PC counter.** Every cycle, all active rows in a column execute their respective instruction for the current PC simultaneously — but each row can have a *different* instruction at that PC address. With 4 columns active, that is up to **16 operations per cycle**.

---

## Memory Layout

### IMEM (Instruction Memory / Context Memory)
- One 128-slot bank **per row** (4 banks total, `CGRA_CMEM_BK_DEPTH = 128`)
- Each slot is one 32-bit instruction word
- `cgra_cmem_init()` always writes the **full** 128 slots per row regardless of kernel size
- For a kernel with N columns and K instructions, each row's IMEM contains:
  ```
  [start_add + 0*K .. start_add + K-1]  → col 0's instructions
  [start_add + 1*K .. start_add + 2K-1] → col 1's instructions
  ...
  ```
  C flat-array index: `imem[row * BKSZ + col * K + pc]`

### KMEM (Kernel Configuration Memory)
- 16 entries (kernel IDs 0–15; ID 0 is reserved/null)
- Each entry is a 16-bit word:
  ```
  bits [15:12] = column bitmask  (which columns are active, e.g. 0xF = all 4)
  bits [11:5]  = IMEM start address (7 bits, 0–127)
  bits [4:0]   = n_instructions - 1 (5 bits → max 32 instructions per kernel)
  ```

---

## Instruction Format

Each instruction is **32 bits**, encoding one RC's operation for one cycle:

```
[31:28]  muxA   (4 bits)  — operand A source
[27:24]  muxB   (4 bits)  — operand B source
[23:19]  ALU op (5 bits)  — operation
[18:17]  dst    (2 bits)  — destination register (R0–R3)
[16]     WE     (1 bit)   — register write enable
[15:13]  muxF   (3 bits)  — flag input source (for BSFA/BZFA)
[12:0]   IMM   (13 bits)  — signed immediate
```

### Operand Sources (muxA / muxB)

| Index | Name   | Meaning                              |
|-------|--------|--------------------------------------|
| 0     | `ZERO` | Constant 0                           |
| 1     | `SELF` | Result of this RC's previous cycle   |
| 2     | `RCL`  | Result from the RC to the **left**   |
| 3     | `RCR`  | Result from the RC to the **right**  |
| 4     | `RCT`  | Result from the RC **above** (top)   |
| 5     | `RCB`  | Result from the RC **below** (bottom)|
| 6–9   | `R0`–`R3` | Local register file               |
| 10    | `IMM`  | Signed 13-bit immediate (sign-extended to 32 bits) |

Neighbour routing is **single-hop only** — you can't chain RCL→RCL in one instruction.

### Flag Sources (muxF — for BSFA/BZFA)

| Name   | Meaning                       |
|--------|-------------------------------|
| `SELF` | Flag from this RC             |
| `RCL`  | Flag from left neighbour      |
| `RCR`  | Flag from right neighbour     |
| `RCT`  | Flag from top neighbour       |
| `RCB`  | Flag from bottom neighbour    |

---

## ALU Operations

### Arithmetic
| Op       | Opcode | Description                                      |
|----------|--------|--------------------------------------------------|
| `NOP`    | 0      | No operation                                     |
| `SADD`   | 1      | Signed add: `A + B`                              |
| `SSUB`   | 2      | Signed subtract: `A - B`                         |
| `SMUL`   | 3      | Signed multiply: `A * B` (lower 32 bits); **stalls column 1 extra cycle** |
| `FXPMUL` | 4      | Fixed-point multiply: `(A * B) >> 16`; **stalls column 1 extra cycle** |
| `SABS`   | 26     | Signed absolute value: `\|A\|` (uses muxA only) |

### Shift
| Op    | Opcode | Description                         |
|-------|--------|-------------------------------------|
| `SLL` | 5      | Shift left: `A << B[4:0]`           |
| `SRL` | 6      | Shift right logical: `A >> B[4:0]`  |
| `SRA` | 7      | Shift right arithmetic: `A >> B[4:0]` (sign-extending) |

> **Shift amount comes from muxB, not IMM.** The lower 5 bits of operand B are the shift amount. To shift by a constant N, you must set `muxB = IMM` with `imm = N`. Setting `muxB = ZERO` gives a shift of 0 (no-op).

### Logical
| Op      | Opcode | Description             |
|---------|--------|-------------------------|
| `LAND`  | 8      | Bitwise AND: `A & B`    |
| `LOR`   | 9      | Bitwise OR: `A \| B`    |
| `LXOR`  | 10     | Bitwise XOR: `A ^ B`    |
| `LNAND` | 11     | Bitwise NAND: `~(A & B)`|
| `LNOR`  | 12     | Bitwise NOR: `~(A \| B)`|
| `LNXOR` | 13     | Bitwise XNOR: `~(A ^ B)`|

### Select (flag-based, no branch)
| Op     | Opcode | Description                                              |
|--------|--------|----------------------------------------------------------|
| `BSFA` | 14     | Select: if **sign flag** (from muxF) is set → A, else B |
| `BZFA` | 15     | Select: if **zero flag** (from muxF) is set → A, else B |

> **Flag timing:** Flags are **registered** (1-cycle delay). The instruction that produces the flag must immediately precede the BSFA/BZFA instruction.

### Control Flow
| Op     | Opcode | Description                                                        |
|--------|--------|--------------------------------------------------------------------|
| `BEQ`  | 16     | Branch if A == B: target = `imm[4:0]`                             |
| `BNE`  | 17     | Branch if A != B: target = `imm[4:0]`                             |
| `BLT`  | 18     | Branch if A < B (signed): target = `imm[4:0]`                     |
| `BGE`  | 19     | Branch if A >= B (signed): target = `imm[4:0]`                    |
| `JUMP` | 20     | Unconditional jump: target = `(A + B)[4:0]` (absolute IMEM index) |
| `EXIT` | 25     | End kernel execution, signal interrupt to CPU                      |

Branch targets are **absolute** IMEM indices within the kernel (relative to `start_add`). Max reachable target: 31 (5-bit field).

> **JUMP constraint:** Because the target is an absolute IMEM index, a JUMP kernel must start at `start_add = 0`.

> **One-hot branch constraint (hardware):** In a multi-row column, branch instructions (BEQ/BNE/BLT/BGE/JUMP) are accepted **only if exactly one row issues a branch request** at that cycle (`cgra_rcs.sv` performs a one-hot check on `rcs_br_req_row_s`). If all N rows execute a branch simultaneously the check fails and the branch is **silently dropped** — execution falls through to the next PC with no error.
>
> **Rule: only one row may execute a branch/jump instruction.** Assign all branch and loop-back JUMP instructions to row 0 only. Rows 1–N-1 must have NOP (`imem = 0`) at those PCs. Row 0's one-hot request is accepted and all rows follow the new PC together.
>
> **Loop pattern for multi-row columns:**
> ```
> PC k:   ROW 0 only — SSUB Rctr, 1 → Rctr   (decrement counter)
> PC k+1: ROW 0 only — BNE(Rctr, 0) → PC_exit (skip jump if done)
> PC k+2: ROW 0 only — JUMP → PC_loop_top      (backward loop)
> PC k+3: all rows   — SWD / EXIT
> ```
> Rows 1–N-1 have NOP at PCs k, k+1, k+2 (leave `imem[II(r,0,k)]` as 0).

### Memory Access
| Op    | Opcode | Description                                                    |
|-------|--------|----------------------------------------------------------------|
| `LWD` | 21     | Load word (direct): addr = `rd_data_cnt`; counter += imm      |
| `SWD` | 22     | Store word (direct): addr = `wr_data_cnt`; counter += imm     |
| `LWI` | 23     | Load word (indirect): addr = `muxB` (register pointer)        |
| `SWI` | 24     | Store word (indirect): addr = `muxB`; data = `muxA`           |

`rd_data_cnt` and `wr_data_cnt` are **per-column** counters initialised from the read/write pointers set by the CPU before launching the kernel. They auto-increment by `imm` bytes on each LWD/SWD.

> **LWD/SWD is per-row sequential, not broadcast.** When N rows in a column all execute LWD/SWD at the same PC, they are serialised via a per-row grant mask. Row 0 accesses `ptr+0`, row 1 `ptr+4`, row 2 `ptr+8`, row 3 `ptr+12` (stride = 4 bytes). The column stalls until all rows have been served. Consequence: provide N_ROW consecutive values in the input buffer per LWD step; to give all rows the same value, replicate it 4×.

---

## Multi-Column Stall and Synchronisation

In a multi-column kernel, stalls affect all columns in two different ways:

### ALU stalls (SMUL / FXPMUL) — merged across all columns
The per-row ALU stall signals are **OR'd across all active columns** in the kernel. If any column has any row executing SMUL, the entire kernel's PC halts for that extra cycle. This keeps all column PCs advancing in lockstep.

### Data stalls (LWD / SWD) — per-column independent
Each column has its own OBI data bus and its own stall. A column's PC halts only while that column's own memory transactions are pending. Other columns continue advancing normally.

> **Practical rule:** All active columns must have the **same number of LWD/SWD instructions at the same PCs**. If columns differ (e.g. one column does 1 LWD and another does 2), they desynchronise after the unequal step and reach EXIT at different times.

### EXIT propagation
When any column executes EXIT, the exec_end signal propagates to all columns in the kernel (via the column access map). Each column transitions to done only when exec_end is asserted **and** it has no pending ALU stall **and** no pending data stall. A column that is mid-LWD/SWD when another column fires EXIT will complete its current memory transaction before stopping; a column mid-SMUL will finish the multiply first. The interrupt to the CPU fires when the first column completes its exit transition.

Practical consequence: if one column exits while others are still doing SWD, the SWDs of the slower columns may not complete before the CPU reads the output buffers. Always design all columns to reach EXIT at the same cycle by equalising their PC structure.

---

## Kernel Assembly Format

Kernels are written as Python files consumed by `cgra_bitstream_gen.py`. Each instruction is a 6-element list:

```python
rcs_instructions[row][imem_addr] = [muxA, muxB, op, dst_reg, muxF, imm]
```

| Field      | Value options                                      | Use `-` for don't-care |
|------------|----------------------------------------------------|------------------------|
| `muxA`     | `ZERO SELF RCL RCR RCT RCB R0 R1 R2 R3 IMM`       | `"-"` → `ZERO`         |
| `muxB`     | same                                               | `"-"` → `ZERO`         |
| `op`       | any op from the ALU table above                    | required               |
| `dst_reg`  | `R0 R1 R2 R3`                                      | `"-"` → WE=0, no write |
| `muxF`     | `SELF RCL RCR RCT RCB`                             | `"-"` → `SELF`         |
| `imm`      | signed integer as string                           | `"-"` → `0`            |

### NOP shorthand
```python
rcs_instructions[row][addr] = rcs_nop_instr
# expands to: ['ZERO', 'ZERO', 'NOP', '-', 'SELF', '0']
```

### Kernel registration
```python
ker_col_needed = 2          # number of active columns (bitmask = 0b0011 = cols 0,1)
ker_num_instr  = 14         # instructions per column (≤ 32)

ker_conf_words[ker_next_id] = get_bin(int(pow(2,ker_col_needed))-1, CGRA_N_COL) + \
                              get_bin(ker_start_add, CGRA_IMEM_NL_LOG2) + \
                              get_bin(ker_num_instr-1, RCS_NUM_CREG_LOG2)

start_add      = ker_start_add
ker_start_add += ker_num_instr * ker_col_needed   # advance IMEM pointer
ker_next_id   += 1
k = ker_num_instr   # shorthand for column offset
```

Then fill `rcs_instructions[row][start_add + col*k + pc]` for every row, column, and PC step.

---

## Execution Model Summary

1. CPU calls `cgra_set_read_ptr()` / `cgra_set_write_ptr()` per active column (column index 0–3)
2. CPU calls `cgra_set_kernel(kid)` to launch kernel ID `kid`
3. CGRA loads KMEM[kid] → gets column mask, start address, instruction count
4. All active columns start at `start_add`; rows within each column execute in lockstep:
   - ALU stalls (SMUL) halt all columns together
   - Data stalls (LWD/SWD) halt only the affected column
5. When any column fires EXIT (and has no pending stall), the interrupt fires and the CPU resumes from `wait_for_interrupt()`

---

## What Code Is a Good Fit

The CGRA excels at **data-parallel loop bodies** with regular memory access patterns:

- **Inner loops of signal processing**: FFT butterfly, FIR filter, convolution
- **Fixed-point linear algebra**: dot products, matrix-vector multiply (FXPMUL-heavy)
- **Data transformations**: permutations, bit-reversal, element-wise ops
- **Streaming pipelines**: each column handles one stage; rows handle N lanes in parallel

### Good mapping indicators
- Loop body fits in ≤32 instructions
- Memory access is strided and predictable (LWD/SWD with fixed stride)
- Same operation applied to multiple independent data elements (rows = parallel lanes)
- Intermediate values can be passed to neighbours (RCL/RCR/RCT/RCB) without going to memory
- All columns have the same number of LWD/SWD steps at the same PCs

### Not a good fit
- Irregular control flow (pointer chasing, recursive algorithms)
- Dynamic memory allocation
- Operations requiring more than 32 instructions per stage
- Kernels needing more than 16 distinct configurations (KMEM depth limit)
- Anything requiring a call stack or function dispatch

---

## Kernel Examples

| Application | Cols | Rows | K  | What it tests / does |
|-------------|------|------|----|----------------------|
| `cgra_alu_test`         | 1 | 1 | varies | All 21 ALU ops, one per kernel |
| `cgra_leftright_test`   | 2 | 1 | 6  | Inter-column data passing via RCL: computes A×B+C |
| `cgra_fullgrid_test`    | 4 | 4 | 6  | All 16 RCs active; 16 distinct functions (arithmetic, power/abs, bitwise, fused-mul) |
| `cgra_load_store_test`  | – | – | –  | LWD/SWD correctness |
| `cgra_func_test`        | 4 | – | 32 | Load/store, arithmetic, multiply — general functionality |
| `cgra_fft`              | 2 | – | 14 | FFT butterfly (complex multiply + add/subtract via RCL/RCR/RCT/RCB) |
| `cgra_dbl_search`       | – | – | –  | Double-search: find min/max |
| `kernel_test`           | – | – | –  | Multi-kernel benchmark (conv, reversebits, bitcount, sqrt, gsm, strsearch, sha, sha2, sabs) |

### Python bitstream examples (`hw/vendor/esl_epfl_cgra/util/`)
| File | What it does |
|------|-------------|
| `instructions_func_test.py`            | Load/store, arithmetic, multiply — general functionality test |
| `instructions_fft_bitrev.py`           | Bit-reversal permutation for FFT input reordering |
| `instructions_fft_cplx.py`            | FFT butterfly |
| `instructions_while_loop_100percent.py`| Infinite loop, 100% utilization (power measurement) |
| `instructions_dbl_min/max.py`          | Double-search: find minimum / maximum |

---

## Resizing the CGRA

The CGRA dimensions are configured at design time via `heepsilon_cfg.hjson`. Rows and columns are **fully independent** — any N×M combination is supported. The following configurations have been validated by running `cgra_check_conf` through Verilator simulation:

| Config | Result |
|--------|--------|
| 4×4    | pass   |
| 8×8    | pass   |
| 4×8 (4 cols, 8 rows) | pass |
| 8×4 (8 cols, 4 rows) | pass |

### Configuration parameters

Edit `heepsilon_cfg.hjson`:

```hjson
cgra: {
    num_columns: 8      // OBI master ports scale with this
    num_rows:    8      // IMEM banks scale with this (one bank per row)
    max_columns: 8      // must equal num_columns (or less to save resources)
                        // "default" means num_columns — only works if you also
                        // set num_columns; otherwise update explicitly
    rcs_num_instr: 32   // instructions per RC per kernel (power of 2, max 32)
    cmem_bk_depth: default  // = max_columns * rcs_num_instr; increase to store more kernels
    kmem_depth: 16
}
```

**Column limit formula** (before the KMEM word overflows 32 bits):
```
max_columns ≤ 2^(32 - log2(rcs_num_instr) - log2(max_columns * rcs_num_instr))
```
With defaults (rcs_num_instr=32): up to **20 columns**.

**Row limit**: no architectural constraint. Each row adds one 32-bit × `cmem_bk_depth` IMEM bank.

### What scales automatically

After changing the config, `make mcu-gen` regenerates all derived files:

| File | What changes |
|------|-------------|
| `hw/vendor/esl_epfl_cgra/hw/rtl/cgra_pkg.sv` | `N_ROW`, `N_COL`, `MAX_COL_REQ`, KMEM word layout, IMEM depth |
| `hw/rtl/heepsilon_pkg.sv` | `CGRA_XBAR_NMASTER = num_columns` — OBI bus port array widens |
| `sw/external/drivers/cgra/cgra.h` | `CGRA_N_COLS`, `CGRA_N_ROWS`, `CGRA_CMEM_BK_DEPTH`, `CGRA_CMEM_TOT_DEPTH` |
| `hw/vendor/esl_epfl_cgra/util/cgra_bitstream_gen.py` | Python bitstream generator constants |

The hardware crossbar and IMEM banking are fully parametric — no manual RTL edits needed.

### Memory banks

The CPU-side RAM must be large enough to hold the `cgra_cmem_bitstream` array, which is `CGRA_CMEM_TOT_DEPTH × 4` bytes = `num_rows × max_columns × rcs_num_instr × 4` bytes.

| Grid  | CGRA_CMEM_TOT_DEPTH | Bitstream size | Recommended MEMORY_BANKS |
|-------|---------------------|----------------|--------------------------|
| 4×4   | 512                 | 2 KB           | 2 (default, 64 KB)        |
| 8×8   | 2048                | 8 KB           | 6 (192 KB)               |

If the linker reports `.bss will not fit in region ram1`, increase `MEMORY_BANKS`.

### Full rebuild procedure

```bash
# 1. Edit heepsilon_cfg.hjson (num_columns, num_rows, max_columns)

# 2. Regenerate RTL and linker script
make mcu-gen MEMORY_BANKS=<N>   # N = number of 32 KB RAM banks needed

# 3. Rebuild the simulator (required — bus port count changed)
make verilator-sim

# 4. Build and run a test
make run-verilator PROJECT=cgra_check_conf
```

> `MEMORY_BANKS` must be passed to **both** `mcu-gen` and the run target, because it controls both the simulated hardware RAM and the linker script that allocates SW data into it. If you run `mcu-gen` with `MEMORY_BANKS=6` the linker script is updated for all subsequent `make app` calls in that session.

### SW compatibility

Applications that are hardcoded for 4×4 will refuse to compile on a different size:
```c
#if CGRA_N_COLS != 4 | CGRA_N_ROWS != 4
  #error The CGRA must have a 4x4 size to run this example
#endif
```
These are: `cgra_fft`, `cgra_func_test`, `cgra_dbl_search`. All other applications in this repo use `CGRA_N_COLS`/`CGRA_N_ROWS` constants and compile on any size.

`cgra_check_conf` is the recommended first validation after any resize — it exercises LWD/SWD and RCT/RCL routing on all RCs without any hardcoded dimension assumptions.

---

## Generating Bitstreams

```bash
cd hw/vendor/esl_epfl_cgra/util/
# Edit cgra_bitstream_gen.py to exec() your instruction file
conda run -n core-v-mini-mcu python cgra_bitstream_gen.py
# Output: ../bitstream/cgra_imem.bit  ../bitstream/cgra_kmem.bit
```

The `.bit` files are then embedded into the SW application's `cgra_imem.h` / `cgra_kmem.h` headers and loaded at runtime via `cgra_cmem_init()`.

Alternatively, kernels can be assembled inline in C using the `INSTR()` macro (see any `sw/applications/cgra_*_test/main.c` for examples). This avoids the Python toolchain for simple test kernels.
