# CPU–CGRA Interface: Addresses, Calls, and Timing

This document describes exactly how the CPU programs, launches, and retrieves results from the CGRA at the register/address level.

---

## Two Distinct Memory Regions

The CGRA occupies two separate address windows:

| Region | Base Address | Size | Purpose |
|--------|-------------|------|---------|
| CGRA IMEM (slave bus) | `0xF000_0000` | `CGRA_SIZE` | Instruction memory (CMEM) + kernel memory (KMEM) — written once at boot |
| CGRA Control Regs (AO peripheral) | `0x2007_0000` | `0x1000` | Launch, pointer, status, perf counters — written per kernel invocation |

`CGRA_PERIPH_START_ADDRESS = AO_PERIPHERAL_START_ADDRESS (0x2000_0000) + 0x0007_0000`

---

## Control Register Map

All offsets are relative to `CGRA_PERIPH_START_ADDRESS`:

| Offset | Register | R/W | Description |
|--------|----------|-----|-------------|
| `0x00` | `COL_STATUS` | R | 4-bit mask: bit N = 1 if column N is busy |
| `0x04` | `KERNEL_ID` | W (launch) / R (poll) | Write kernel ID (1–15) to launch; reads 0 when CGRA is idle |
| `0x08` | `PTR_IN_COL_0` | W | Input data pointer for column 0 |
| `0x0C` | `PTR_OUT_COL_0` | W | Output data pointer for column 0 |
| `0x10` | `PTR_IN_COL_1` | W | Input data pointer for column 1 |
| `0x14` | `PTR_OUT_COL_1` | W | Output data pointer for column 1 |
| `0x18` | `PTR_IN_COL_2` | W | Input data pointer for column 2 |
| `0x1C` | `PTR_OUT_COL_2` | W | Output data pointer for column 2 |
| `0x20` | `PTR_IN_COL_3` | W | Input data pointer for column 3 |
| `0x24` | `PTR_OUT_COL_3` | W | Output data pointer for column 3 |
| `0x28` | `PERF_CNT_ENABLE` | W | Bit 0: enable performance counters |
| `0x2C` | `PERF_CNT_RESET` | W | Bit 0: reset all performance counters (self-clears) |
| `0x30` | `PERF_CNT_TOTAL_KERNELS` | R | Total kernels completed since last reset |
| `0x34` | `PERF_CNT_COL_0_ACTIVE` | R | Active cycles for column 0 |
| `0x38` | `PERF_CNT_COL_0_STALL` | R | Stall cycles for column 0 |
| `0x3C` | `PERF_CNT_COL_1_ACTIVE` | R | Active cycles for column 1 |
| `0x40` | `PERF_CNT_COL_1_STALL` | R | Stall cycles for column 1 |
| `0x44` | `PERF_CNT_COL_2_ACTIVE` | R | Active cycles for column 2 |
| `0x48` | `PERF_CNT_COL_2_STALL` | R | Stall cycles for column 2 |
| `0x4C` | `PERF_CNT_COL_3_ACTIVE` | R | Active cycles for column 3 |
| `0x50` | `PERF_CNT_COL_3_STALL` | R | Stall cycles for column 3 |

Column N pointer registers are always at `PTR_IN_COL_0 + N*0x8` and `PTR_OUT_COL_0 + N*0x8`.

---

## IMEM / KMEM Layout at 0xF000_0000

Written once by `cgra_cmem_init()`. The layout is:

```
0xF000_0000  ┌──────────────────────────┐
             │  CMEM bank 0 (row 0)     │  128 × 32-bit words = 512 bytes
0xF000_0200  ├──────────────────────────┤
             │  CMEM bank 1 (row 1)     │  128 × 32-bit words
0xF000_0400  ├──────────────────────────┤
             │  CMEM bank 2 (row 2)     │  128 × 32-bit words
0xF000_0600  ├──────────────────────────┤
             │  CMEM bank 3 (row 3)     │  128 × 32-bit words
0xF000_0800  ├──────────────────────────┤
             │  KMEM (kernel config)    │  16 × 32-bit words = 64 bytes
0xF000_0840  └──────────────────────────┘
```

Bank stride = `1 << CGRA_CMEM_BK_DEPTH_LOG2` = `1 << 7` = 128 words = 0x200 bytes.
KMEM starts at `0xF000_0000 + 4 * 0x200 = 0xF000_0800`.

`cgra_cmem_init` writes these via plain 32-bit MMIO stores through the OBI external slave bus.

---

## Full Invocation Sequence (Timing Diagram)

```
CPU                                     CGRA HW
───────────────────────────────────────────────────────────────────────
BOOT / ONE-TIME SETUP
──────────────────────────────────────────────────────────────────────
1. cgra_cmem_init(imem, kmem)
   │  for each row r in [0..3]:
   │    store imem[r*128..r*128+127]
   │    → 0xF000_0000 + r*0x200          ──────────────────────────►  CMEM loaded
   │  store kmem[0..15]
   │    → 0xF000_0800                    ──────────────────────────►  KMEM loaded

2. Interrupt wiring (PLIC + CSR)
   │  plic_irq_set_priority(CGRA_INTR, 1)
   │  plic_irq_set_enabled(CGRA_INTR, true)
   │  CSR mstatus.MIE = 1
   │  CSR meie = 1

PER-KERNEL INVOCATION
──────────────────────────────────────────────────────────────────────
3. cgra_wait_ready(&cgra)
   │  poll: rd 0x2007_0004 (KERNEL_ID)
   │  └─ loop until value == 0           ──── KERNEL_ID reads back 0 when idle

4. cgra_perf_cnt_enable(&cgra, 1)        (optional)
   │  wr 0x2007_0028 ← 1

5. cgra_set_read_ptr(&cgra, ptr, col)
   │  col 0: wr 0x2007_0008 ← ptr0      ──────────────────────────►  rd_ptr[0] latched
   │  col 1: wr 0x2007_0010 ← ptr1      ──────────────────────────►  rd_ptr[1] latched
   │  col 2: wr 0x2007_0018 ← ptr2      ──────────────────────────►  rd_ptr[2] latched
   │  col 3: wr 0x2007_0020 ← ptr3      ──────────────────────────►  rd_ptr[3] latched

   (optional) cgra_set_write_ptr(&cgra, ptr, col)
   │  col 0: wr 0x2007_000C ← out0      ──────────────────────────►  wr_ptr[0] latched
   │  ...

6. cgra_set_kernel(&cgra, KERNEL_ID)
   │  wr 0x2007_0004 ← kernel_id        ──────────────────────────►  acc_req asserted
   │                                                                   CGRA reads KMEM[kernel_id]
   │                                                                   KERNEL_ID reg cleared to 0
   │                                                                   col_status bits set
   │                                                                   kernel begins executing:
   │                                                                    - fetches instr from CMEM
   │                                                                    - issues LWD via OBI to SRAM
   │                                                                    - issues SWD via OBI to SRAM
   │                                                                    - executes ALU ops
   │                                                                    - on EXIT: acc_end asserted

7. CPU waits for interrupt
   │  while(!cgra_intr_flag) wfi()
   │                                     ◄──────────── EXT_INTR_0 fires (acc_end)
   │  cgra_intr_flag = 1

8. Read results from SRAM
   │  (output arrays already in CPU SRAM, written by CGRA SWD instructions)

OPTIONAL: PERFORMANCE COUNTER READOUT
──────────────────────────────────────────────────────────────────────
9. cgra_perf_cnt_get_kernel(&cgra)
   │  rd 0x2007_0030  → total kernels executed

   cgra_perf_cnt_get_col_active(&cgra, col)
   │  col 0: rd 0x2007_0034
   │  col 1: rd 0x2007_003C
   │  col 2: rd 0x2007_0044
   │  col 3: rd 0x2007_004C

   cgra_perf_cnt_get_col_stall(&cgra, col)
   │  col 0: rd 0x2007_0038
   │  ...

   cgra_perf_cnt_reset(&cgra)
   │  wr 0x2007_002C ← 1                ──────────────────────────►  all counters → 0
```

---

## What Happens Inside the CGRA on `cgra_set_kernel()`

1. The write to `KERNEL_ID` asserts `acc_req` internally.
2. `peripheral_regs` acknowledges (`acc_ack`) and sets `col_status` bits for the enabled columns.
3. The CGRA controller fetches `KMEM[kernel_id]` to find: which columns to activate, CMEM start address, instruction count.
4. Each active column starts fetching instructions from its CMEM bank (base `CMEM_start + col * instr_count`).
5. LWD/SWD instructions make OBI bus transactions to CPU SRAM via the external slave bus.
6. When every active column executes EXIT, `acc_end` is asserted, `col_status` bits are cleared, and `EXT_INTR_0` fires.

---

## Key Points

- **Write pointers are optional**: if all outputs go through SWD with addresses embedded in the input array, `cgra_set_write_ptr` is never called (as in `cgra_func_test`).
- **`cgra_wait_ready` polls `KERNEL_ID`**: it reads 0 when idle because `peripheral_regs` auto-clears the register on `acc_ack`. This is not the same as `COL_STATUS` (which stays set until kernel finishes).
- **Input pointer is a pointer-to-array**: each row's LWD fetches sequential words from the address stored in `PTR_IN_COL_N`. There is no scatter/gather — the kernel itself controls offsets via the immediate field of LWD.
- **No DMA**: all IMEM writes and all LWD/SWD data transfers go through the same OBI bus. The CPU and CGRA share the bus and may stall each other.
