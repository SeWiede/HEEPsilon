# CGRA Cooperative Preemption

This document describes how to preempt (interrupt/abort) a running CGRA kernel and the architectural constraints involved.

---

## Overview

The CGRA has no hardware preemption mechanism — there is no CPU-visible register to forcibly halt execution. The only way to stop a running kernel is **cooperative preemption**: the kernel itself polls a flag in shared SRAM each loop iteration and voluntarily exits when the flag is set.

This mirrors the "FLEP" approach used in GPU research, adapted to the shared-SRAM, OBI-bus architecture of HEEPsilon.

---

## Key Architectural Constraint: Sequential LWD

OpenEdgeCGRA's `LWD` instruction always advances the read pointer sequentially — one word per call, forward only. **The kernel cannot poll one fixed address repeatedly.** Each iteration reads the next word in the flag buffer.

This means:
- There is no single "interrupt address" the CPU writes once to stop the CGRA.
- The preempt flag must be written to the slot the CGRA is *about to read*.
- The CPU must track the current CGRA iteration (e.g., via the output buffer) to know the right slot.

This is the difference from a hardware interrupt register; it is a deliberate software-cooperative design.

---

## How It Works

### Separate Flag Buffer

The cleanest design uses a **dedicated flag buffer** (not interleaved with data):

```c
int32_t flag_buf[MAX_ITER + 1];   // all zeros at launch
int32_t output_buf[MAX_ITER];

cgra_set_read_ptr (&cgra, (uint32_t)flag_buf,   col);
cgra_set_write_ptr(&cgra, (uint32_t)output_buf, col);
```

The flag buffer IS the preemption memory region. Slot `MAX_ITER` is always 1 (natural-completion sentinel).

### Kernel Design

```
PC 0: LWD R1           ; read next flag word from flag_buf (sequential)
PC 1: BNE(R1, 0) → 5  ; exit if flag ≠ 0
PC 2: <work>           ; normal kernel computation (register-based)
PC 3: SWD              ; write output
PC 4: JUMP 0           ; loop back
PC 5: EXIT
```

### Triggering Preemption: One Store

The CPU watches the output buffer to determine when the CGRA completes iteration `N`:

```c
while (output_buf[N - 1] == 0) {}   // observe: wait for iteration N-1 to complete

flag_buf[N + 2] = 1;                // one store — preempt at iteration N+2
```

**Why N+2?** After the CGRA writes `output_buf[N-1]` it needs ~12 cycles to reach `flag_buf[N+2]` (JUMP + LWD + BNE + work + SWD + JUMP + LWD). The CPU reacts in ~6 cycles (poll detects write + issues store). CPU wins with margin.

Writing to N+1 is too tight (~6 cycles for CGRA vs ~6 for CPU). N+2 is reliable.

---

## Verified Behavior

Tested in `sw/applications/cgra_loop_preempt` — a sweep where CPU waits for N CGRA iterations to complete, then issues one store to `flag_buf[N+2]`:

```
cpu_delay=0   cgra_iters=2
cpu_delay=5   cgra_iters=7
cpu_delay=10  cgra_iters=12
cpu_delay=20  cgra_iters=22
cpu_delay=30  cgra_iters=32
cpu_delay=50  cgra_iters=52   (sentinel fires: all 50 iterations + 2)
```

`cgra_iters = cpu_delay + 2` exactly — proportional, deterministic, 0 errors. The CGRA runs in parallel and is stopped on demand by a **single CPU store**.

---

## Timing Notes

The CGRA iterates much faster than a CPU busy-loop (~10:1 in Verilator). This means:
- The CPU cannot estimate the CGRA's iteration count from its own loop counter.
- The output buffer is the reliable signal: `output_buf[N] != 0` means iteration N is done.
- Writing at an absolute position without observing output (e.g., `flag_buf[delay * 10 + 2]`) would require knowing the CPU/CGRA speed ratio, which may vary with workload.

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

## Flag Buffer Layout

```
flag_buf[0]           = 0      ; run
flag_buf[1]           = 0      ; run
...
flag_buf[N + 2]       = 1      ; CPU writes here to preempt at iteration N+2
...
flag_buf[MAX_ITER]    = 1      ; sentinel — kernel never overruns the buffer
```

No data is interleaved with the flags. The kernel's computation uses only registers and the write pointer (`output_buf`). This keeps the preemption region clean: one contiguous zero-filled array that the CPU targets with one store.
