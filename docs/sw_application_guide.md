# HEEPsilon SW Application Guide

This document covers the software side of HEEPsilon: the structure of applications, how to write a new one from scratch using inline CGRA bitstreams, and a complete catalog of every application in `sw/applications/`.

---

## Application Catalog

| Application | CGRA? | Purpose |
|---|---|---|
| `cgra_alu_test` | yes | Tests all 21+ ALU ops (SADD, SSUB, SMUL, FXPMUL, SABS, shifts, logic, branches, JUMP, LWD/SWD) |
| `cgra_leftright_test` | yes | Tests inter-column data passing via RCL: computes A×B+C using cols 0 and 1 |
| `cgra_fullgrid_test` | yes | Full 4×4 grid (16 RCs active): 4 distinct column functions × 4 row variants |
| `cgra_check_conf` | yes | First-boot sanity check: LWD/SWD + RCT/RCL routing, works on any CGRA size |
| `cgra_load_store_test` | yes | LWD/SWD correctness with known input/output arrays |
| `cgra_func_test` | yes | General functionality: load/store + arithmetic + multiply; hardcoded for 4×4 |
| `cgra_fir` | yes | 4-tap FIR filter `y[n]=Σh[k]x[n-k]`; all 4 columns active, 1 output sample per column per call |
| `cgra_fft` | yes | FFT butterfly; hardcoded for 4×4 |
| `cgra_dbl_search` | yes | Finds minimum and maximum in an array; hardcoded for 4×4 |
| `kernel_test` | yes | Multi-kernel benchmark harness using the `kcom` framework (see `kernel_test_guide.md`) |
| `mmul_os` | yes | Output-stationary matrix multiply on CGRA |
| `transformer` | yes | Transformer model inference with CGRA acceleration |
| `transformer_without_cgra` | no | Same transformer inference, CPU only (for comparison) |
| `trans_versasense` | yes | Transformer inference variant for VersaSense sensor data |

### Which app to start with

- **First time on the platform**: `cgra_check_conf` — runs on any grid size, immediately confirms LWD/SWD and neighbour routing work.
- **Testing a specific op**: `cgra_alu_test` — all opcodes with checked results.
- **Benchmarking a kernel**: `kernel_test` with `kcom` harness.
- **Reference for a new kernel**: `cgra_fir` — clean, well-commented inline bitstream for a real DSP kernel.

---

## Address Map (from `heepsilon.h`)

> For the full CPU address space, memory bank explanation, bus topology, and SRAM vs DRAM rationale see [`memory_architecture.md`](memory_architecture.md).

```c
// sw/external/extensions/heepsilon.h
#define CGRA_START_ADDRESS   (EXT_SLAVE_START_ADDRESS  + 0x000000)  // context memory
#define CGRA_PERIPH_START_ADDRESS (EXT_PERIPHERAL_START_ADDRESS + 0x0000000)  // control regs
```

- `CGRA_START_ADDRESS` — write IMEM/KMEM bitstreams here via `cgra_cmem_init()`.
- `CGRA_PERIPH_START_ADDRESS` — read/write CGRA control registers (`CGRA_KERNEL_ID`, `CGRA_PTR_IN_COL_n`, etc.) here.

The `cgra_t` struct wraps the peripheral base address:
```c
cgra_t cgra;
cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);
```

---

## Boilerplate: Minimum New Application

Every CGRA application needs four things: includes, interrupt handler, `cgra_t` init, and an interrupt flag. The pattern used across all applications in this repo:

### 1. Includes

```c
#include "csr.h"
#include "hart.h"
#include "handler.h"
#include "core_v_mini_mcu.h"
#include "rv_plic.h"
#include "rv_plic_regs.h"
#include "heepsilon.h"    // CGRA_START_ADDRESS, CGRA_PERIPH_START_ADDRESS
#include "cgra.h"         // cgra_t, cgra_set_read_ptr, cgra_set_kernel, etc.
```

### 2. Interrupt handler

```c
static volatile int8_t cgra_intr_flag;

// Called by the PLIC dispatcher when EXT_INTR_0 fires
void handler_irq_cgra(uint32_t id) {
    cgra_intr_flag = 1;
}
```

The function name `handler_irq_cgra` is looked up by the X-HEEP interrupt dispatch table; do not rename it.

### 3. `cgra_t` init and interrupt enable (in `main`)

```c
cgra_t cgra;
cgra.base_addr = mmio_region_from_addr((uintptr_t)CGRA_PERIPH_START_ADDRESS);

// Enable CGRA interrupt in the PLIC
rv_plic_irq_set_priority(CGRA_INTR, 1);
rv_plic_irq_set_ie(CGRA_INTR, true);
rv_plic_set_threshold(CGRA_INTR, 0);
CSR_SET_BITS(CSR_REG_MSTATUS, 0x8);   // mstatus.MIE = 1
CSR_WRITE(CSR_REG_MIE, (1 << 11));    // meie = 1 (enable machine external interrupts)
```

### 4. Launch and wait

```c
cgra_intr_flag = 0;

cgra_set_read_ptr (&cgra, (uint32_t)input_array,  column_idx);
cgra_set_write_ptr(&cgra, (uint32_t)output_array, column_idx);
cgra_set_kernel   (&cgra, kernel_id);

// Spin until interrupt fires (use wfi in production; spin for sim clarity)
while (!cgra_intr_flag) { }
```

---

## The `INSTR()` Macro

All test applications encode CGRA instructions inline in C using this macro (copy-pasted into each app's `main.c`):

```c
#define INSTR(ma, mb, op, rs, we, fs, imm) \
    (  ((uint32_t)((ma)  & 0xF ) << 28) \
     | ((uint32_t)((mb)  & 0xF ) << 24) \
     | ((uint32_t)((op)  & 0x1F) << 19) \
     | ((uint32_t)((rs)  & 0x3 ) << 17) \
     | ((uint32_t)((we)  & 0x1 ) << 16) \
     | ((uint32_t)((fs)  & 0x7 ) << 13) \
     | ((uint32_t)((imm) & 0x1FFF)    ))
```

| Arg | Bits     | Meaning |
|-----|----------|---------|
| `ma` | [31:28] | mux_a source (0=zero, 1=own_res, 2=left, 3=right, 4=top, 5=bot, 6–9=R0–R3, 10=imm) |
| `mb` | [27:24] | mux_b source (same encoding) |
| `op` | [23:19] | ALU opcode (see `cgra_isa.md` for full table) |
| `rs` | [18:17] | destination register index (0–3) |
| `we` | [16]    | 1 = write result to `reg[rs]` |
| `fs` | [15:13] | flag mux select (for BSFA/BZFA) |
| `imm`| [12:0] | signed 13-bit immediate (stride in bytes for LWD/SWD, coefficient for SMUL, shift amount source for SLL/SRL/SRA) |

Common source constants used in applications:
```c
#define SRC_ZERO  0
#define SRC_OWN   1
#define SRC_R0    6
#define SRC_R1    7
#define SRC_R2    8
#define SRC_R3    9
#define SRC_IMM   10
```

A NOP is `0x00000000` (all fields zero, opcode=NOP=0, WE=0).

---

## IMEM Array Indexing

Applications that build bitstreams inline allocate:

```c
static uint32_t imem[CGRA_CMEM_TOT_DEPTH];   // N_ROWS × CMEM_BK_DEPTH words
static uint32_t kmem[CGRA_KMEM_DEPTH];       // 16 kernel config words
```

The flat IMEM index for row `r`, column `c`, PC step `p` with kernel instruction count `K` starting at `start_add = 0`:

```c
#define BKSZ  CGRA_CMEM_BK_DEPTH   // 128
#define II(r, c, p)  ((r)*BKSZ + (c)*K + (p))
```

Every slot not explicitly set should be zeroed (`memset(imem, 0, sizeof(imem))`). Uninitialised slots contain garbage and will execute as random instructions.

---

## KMEM Word Helper

```c
#define KMEM_WORD(cols, start, n) \
    (((uint32_t)(cols) << 12) | ((uint32_t)(start) << 5) | ((uint32_t)(n) - 1))
```

| Arg | Bits | Meaning |
|-----|------|---------|
| `cols` | [15:12] | one-hot column mask (e.g. `0xF` = all 4, `0x1` = col 0 only) |
| `start` | [11:5] | start address in IMEM bank (0–127) |
| `n` | [4:0]+1 | number of instructions (1–32; field stores n−1) |

Example — kernel ID 1, all 4 columns, start at 0, 15 instructions:
```c
kmem[1] = KMEM_WORD(0xF, 0, 15);
```

Load both memories before setting any pointers:
```c
cgra_cmem_init(imem, kmem);
```

---

## Data Alignment

Input and output arrays passed to `cgra_set_read_ptr` / `cgra_set_write_ptr` must be 4-byte aligned:

```c
static int32_t input[N]  __attribute__((aligned(4)));
static int32_t output[N] __attribute__((aligned(4)));
```

`cgra_cmem_init` also requires the `imem` array to be word-aligned (stack allocation is fine; global static is always aligned).

---

## Building and Running

```bash
make app           PROJECT=<app_name>          # compile only → sw/build/main.hex
make run-verilator PROJECT=<app_name>          # compile + simulate; output → uart0.log
make run-questasim PROJECT=<app_name>
make run-fpga      PROJECT=<app_name>          # flash PYNQ-Z2
make run-fpga-com  PROJECT=<app_name>          # flash + open picocom console
```

`make app` discovers the application by looking for `sw/applications/<app_name>/main.c`. There is no separate `CMakeLists.txt` or registration step — the directory name is the project name.

---

## Adding a New Application

1. Create `sw/applications/my_kernel/main.c`.
2. Copy the boilerplate above (includes, interrupt handler, `cgra_t` init, interrupt enable).
3. Declare `imem[]` and `kmem[]`, fill them with `INSTR()` calls and `KMEM_WORD()`.
4. Call `cgra_cmem_init(imem, kmem)`.
5. Set pointers, launch, wait for interrupt.
6. Run with `make run-verilator PROJECT=my_kernel`.

No Makefile modifications needed. The X-HEEP build system picks up any directory under `sw/applications/` automatically.
