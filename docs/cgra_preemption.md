# CGRA Cooperative Preemption

This document describes how to preempt (interrupt/abort) a running CGRA kernel and the architectural constraints involved.

---

## Overview

The CGRA has no hardware preemption mechanism — there is no CPU-visible register to forcibly halt execution. The only way to stop a running kernel is **cooperative preemption**: the kernel itself polls a flag in shared SRAM each loop iteration and voluntarily exits when the flag is set.

This mirrors the "FLEP" approach used in GPU research, adapted to the shared-SRAM, OBI-bus architecture of HEEPsilon.

---

## How It Works

### Shared SRAM as the Signaling Channel

CPU and CGRA share the same 6-bank × 32 kB SRAM. The CGRA accesses it via LWD/SWD instructions through the OBI external slave bus — the same bus used by the CPU for loads and stores.

This means a CPU write to any SRAM address is immediately visible to the CGRA on its next LWD from that address. No special IPC mechanism is needed.

### Kernel Design for Preemptability

A preemptable looping kernel adds two instructions at the top of its loop body:

```
PC 0: LWD R0           ; read preempt flag from input stream
PC 1: BNE(R0, 0) → EXIT ; exit if flag ≠ 0
PC 2: ...              ; normal kernel work
...
PC N: JUMP 0           ; loop back
PC M: EXIT             ; exit target for BNE
```

The input array is laid out as `[flag, data0, data1, ...]` per iteration. Setting `flag ≠ 0` at iteration `i` causes the kernel to exit at the start of that iteration, after completing all previous iterations cleanly.

### Deterministic vs. Asynchronous Preemption

| Mode | How | When to use |
|------|-----|-------------|
| **Pre-set (deterministic)** | Write `flag[i] = 1` in the input array *before* launch | Known preemption point, benchmarking, sweep tests |
| **Asynchronous** | Write `flag[i] = 1` in SRAM *after* launch from the CPU | True runtime interruption; ±1 iteration uncertainty |

The asynchronous case has a race: if the CGRA is between the flag-check and the JUMP when the CPU writes, the abort takes effect one iteration later. This is unavoidable without hardware support.

---

## Verified Behavior

Tested in `sw/applications/cgra_loop_preempt` — a sweep over preemption points {0, 2, 5, 10, 15, 20} on a looping SADD kernel (MAX_ITER=20):

```
preempt@0 : cpu_count=2   completed_iters=0
preempt@2 : cpu_count=3   completed_iters=2
preempt@5 : cpu_count=6   completed_iters=5
preempt@10: cpu_count=11  completed_iters=10
preempt@15: cpu_count=16  completed_iters=15
preempt@20: cpu_count=21  completed_iters=20   (natural completion via sentinel)
```

Key observations:
- **CPU count ≈ preempt_at + 1**: almost exactly one CPU busy-loop iteration per CGRA kernel iteration, showing tight CPU/CGRA parallelism on this workload.
- **No result over-run**: outputs beyond the preemption point remain zero — the kernel exits cleanly before writing them.
- **Clean re-launch**: the CGRA can be re-launched immediately after each preemption via `cgra_wait_ready()` + `cgra_set_kernel()`. No reset required.
- **Total errors: 0** across all 6 runs.

---

## Architectural Constraints

### 1. No State Save on Preemption

There is no mechanism to checkpoint the CGRA's register file or program counter. Preemption is "abort and discard" — partially-completed iterations produce no output. If resumption is needed, the software layer must track which iterations completed (via the output array or a counter) and re-launch from that point.

### 2. CMEM Is Safe to Overwrite During Execution

The CGRA reads CMEM (instruction memory) only during the **CONF phase** (kernel load), not during **EXEC**. During execution the CGRA operates entirely from its local `conf_reg_file`. This means:

- The CPU (or DMA) can write a new kernel to CMEM while the current kernel runs.
- When the current kernel exits (via preemption or natural completion), the new kernel is already in place and can be launched immediately.

This enables a **preempt + hot-swap** pattern:
1. Set preempt flag → current kernel exits at next poll point.
2. (Optionally, in parallel) Write new kernel to CMEM via direct MMIO or DMA.
3. Launch new kernel via `cgra_set_kernel(new_id)`.

### 3. Cross-Column Preemption Requires Software Coordination

If multiple columns are active, there is no hardware signal to stop all of them simultaneously. Each column runs independently and must check the preempt flag individually. A typical pattern:

- **Column 0** checks the external (CPU-written) preempt flag. On detection, it writes a shared `local_stop` word to SRAM and EXITs.
- **Columns 1–3** check `local_stop` at the start of each iteration. They EXIT when it becomes non-zero.

This introduces a lag of up to one iteration between the first column detecting the flag and the others stopping.

### 4. Only the Active Column's Rows Are Preemptable

Within a column, all rows execute the same instruction stream. If a column has `N_ROWS=4` active rows, all 4 rows execute the flag-check LWD simultaneously (each reading from its own slot in the input buffer). There is no mechanism to have "row 0 checks the flag but rows 1–3 don't" — all rows in a column share one program counter.

---

## OBI Bus Contention During Preemption

The CPU's preempt flag write and the CGRA's LWD reads compete on the same OBI bus. The arbiter handles this transparently — neither side stalls indefinitely — but the CPU write may be delayed by in-flight CGRA bus transactions. This is a source of the ±1 iteration uncertainty in asynchronous preemption.

---

## Input Array Layout for Preemptable Kernels

```
input_a[i*STRIDE + 0] = preempt_flag   ; 0 = continue, ≠0 = exit before this iter
input_a[i*STRIDE + 1] = data_arg_0
input_a[i*STRIDE + 2] = data_arg_1
...
input_a[MAX_ITER*STRIDE + 0] = 1       ; natural-completion sentinel (always set)
```

The sentinel at slot `MAX_ITER` ensures the kernel never runs past the valid data range even if no explicit preemption is requested.
