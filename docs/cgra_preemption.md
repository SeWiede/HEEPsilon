# CGRA Cooperative Preemption

This document describes how to preempt (interrupt/abort) a running CGRA kernel and the architectural constraints involved.

---

## Overview

The CGRA has no hardware preemption mechanism — there is no CPU-visible register to forcibly halt execution. The only way to stop a running kernel is **cooperative preemption**: the kernel itself polls a flag in shared SRAM each loop iteration and voluntarily exits when the flag is set.

This mirrors the "FLEP" approach used in GPU research, adapted to the shared-SRAM, OBI-bus architecture of HEEPsilon.

---

## How It Works: LWI for Fixed-Address Polling

OpenEdgeCGRA has two load instructions:

| Instruction | Address source | Sequential pointer |
|-------------|---------------|-------------------|
| `LWD` | `rd_data_cnt` (sequential counter) | **advances** each call |
| `LWI` | `muxB` register (any register) | **never advances** |

`LWI` is the key. The kernel loads `&preempt_flag` into a register once at startup via `LWD`, then polls it every iteration via `LWI` — the same physical address, re-read each loop, with no pointer advancement.

### Kernel Design

```
PC 0: LWD R1           ; load &preempt_flag from input (sequential, once)
PC 1: LWI R0, [R1]     ; R0 = *R1 = preempt_flag  ← fixed address, no pointer advance
PC 2: BNE(R0, 0) → 6  ; exit if preempt_flag ≠ 0
PC 3: SADD(R2,1)→R2   ; R2++ (work)
PC 4: SWD R2           ; write output
PC 5: JUMP 1           ; loop back to LWI
PC 6: EXIT
```

### Triggering Preemption: One Store

```c
preempt_flag = 1;   // that's it — CGRA stops within one iteration
```

No slot tracking. No output polling. No iteration knowledge. The CPU writes to one fixed address at any time and the CGRA stops at its next flag check.

---

## Verified Behavior

Tested in `sw/applications/cgra_loop_preempt` — CPU busy-loops for DELAY iterations then writes `preempt_flag = 1`:

```
cpu_delay=0     cgra_iters=5     preempt_cy=178
cpu_delay=50    cgra_iters=72    preempt_cy=178
cpu_delay=150   cgra_iters=205   preempt_cy=178
cpu_delay=300   cgra_iters=405   preempt_cy=178
cpu_delay=500   cgra_iters=672   preempt_cy=178
```

Proportional preemption, 0 errors. Measured 2026-09-04 on Verilator 5.040 and on
the ZCU104 (4x4 grid, 6 memory banks). **Both produce bit-identical numbers in
every row and both columns.**

The CPU sets the flag at any time — the CGRA stops within one iteration
regardless of when.

### Why the iteration counts moved

An earlier, uninstrumented version of this test reported `cgra_iters` of
0/67/200/400/667 — five lower in every row. The difference is the cost of the
`rv_timer` read taken immediately before the store: it delays the flag write by
roughly five CGRA iterations. That is measurement overhead in the iteration
column, not a change in behaviour.

---

## Preemption Latency

`preempt_cy` is **178 cycles, constant** across every delay in the sweep, in
simulation and on hardware alike.

**This is an upper bound on the CGRA's stop latency, not the stop latency
itself.** The measured interval spans:

```
store preempt_flag = 1
  -> CGRA reaches its next LWI, sees the flag, takes BNE to EXIT
  -> acc_end asserted, col_status cleared
  -> EXT_INTR_0 -> PLIC -> interrupt dispatch
  -> handler_irq_cgra() sets cgra_done
  -> polling loop in main() observes it, second rv_timer read
```

Everything from `acc_end` onward is CPU-side overhead the CGRA does not control.
Software cannot separate the two halves: the CGRA performance counters report
`col_active` only from kernel start — and `col_active` already includes stall
cycles — never time-since-flag. Isolating the CGRA-internal component requires
hardware support.

On the constancy: the variable part of this interval (where in the 5-instruction
kernel loop the flag write lands) is at most a few cycles, while the fixed part
(EXIT, `acc_end`, PLIC, dispatch) dominates. Whether the total is genuinely
phase-locked or the jitter is simply too small to surface at this resolution has
**not** been established — the sweep varies only the CPU delay, which may not
decorrelate the two loops.

178 cycles is the end-to-end number any hardware preemption mechanism has to
beat, measured on the same path.

---

## Timing Notes

The CGRA iterates faster than a CPU busy-loop on this workload (~4:3 ratio). This
was originally recorded as a Verilator observation; the ZCU104 run reproduces it
exactly, so it is not a simulation artefact. The exact ratio depends on the
kernel's memory access pattern.

---

## Architectural Constraints

### 1. No State Save on Preemption

There is no mechanism to checkpoint the CGRA's register file or program counter. Preemption is "abort and discard" — partially-completed iterations produce no output. If resumption is needed, the software layer must track which iterations completed (via the output buffer) and re-launch from that point.

### 2. CMEM Is Safe to Overwrite During Execution

The CGRA reads CMEM (instruction memory) only during the **CONF phase** (kernel load). During EXEC it operates entirely from its local `conf_reg_file`. This means:

- The CPU (or DMA) can write a new kernel to CMEM while the current kernel runs.
- When the current kernel exits, the new kernel is already in place.

This enables a **preempt + hot-swap** pattern:
1. Preempt current kernel via output-observed flag write.
2. (In parallel) Write new kernel to CMEM via MMIO or DMA.
3. Launch new kernel via `cgra_set_kernel(new_id)`.

### 3. Cross-Column Preemption Requires Software Coordination

If multiple columns are active, each column must check the flag independently — there is no hardware broadcast. A typical multi-column pattern:

- **Column 0** checks an external (CPU-written) flag. On detection it writes a shared `local_stop` word to SRAM and EXITs.
- **Columns 1–3** check `local_stop` each iteration. They EXIT when they see it.

This introduces a lag of up to one iteration between the first column detecting the flag and the others stopping.

### 4. All Rows in a Column Share One Program Counter

Within a column, all rows execute the same instruction stream. There is no mechanism to have row 0 check the flag while rows 1–3 skip the check — all rows in a column execute the flag-check LWD simultaneously (each reading from their own sequential slot).

---

## Memory Layout

```c
volatile int32_t preempt_flag = 0;      // THE preemption address — one word
int32_t          input_buf[1]  = { (int32_t)&preempt_flag };
volatile int32_t output_buf[N];         // CGRA writes results here sequentially

cgra_set_read_ptr (&cgra, (uint32_t)input_buf,  col);
cgra_set_write_ptr(&cgra, (uint32_t)output_buf, col);
```

The input buffer contains only the address of `preempt_flag`. The kernel loads it once via `LWD`, stores it in `R1`, and then polls `*R1` via `LWI` every iteration. `preempt_flag` itself is not part of any sequential stream.
