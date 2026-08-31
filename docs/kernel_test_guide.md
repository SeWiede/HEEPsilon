# kernel_test / kcom Framework Guide

`kernel_test` is a benchmarking harness that measures CPU vs. CGRA cycle counts across multiple iterations, computes statistics (mean, standard deviation), and optionally checks result correctness. It is driven by the `kcom` (Kernel Common) library in `sw/applications/kernel_test/kernels_common/`.

---

## `kcom_kernel_t` — The Kernel Descriptor

Every kernel registers itself as a `kcom_kernel_t`:

```c
// sw/applications/kernel_test/kernels_common/kernels_common.h
typedef struct {
    kcom_mem_t  kmem;       // pointer to the kernel's KMEM array
    kcom_mem_t  imem;       // pointer to the kernel's IMEM array
    kcom_io_t   input;      // pointer to input data array (int32_t*)
    kcom_io_t   output;     // pointer to output data array (int32_t*)
    uint8_t     col_n;      // number of active columns (used for pointer setup)
    uint8_t     in_n;       // number of input words per column
    uint8_t     out_n;      // number of output words per column
    void (*config)(void);   // called before each iteration: fills input[], sets read/write ptrs
    void (*func)  (void);   // CPU reference implementation of the same computation
    uint32_t (*check)(void);// compares CGRA output[] against CPU reference; returns error count
    int8_t name[20];        // string printed in the output header
} kcom_kernel_t;
```

The harness calls these three callbacks in order each iteration:
1. `config()` — fills `input[]` with fresh data, calls `cgra_set_read_ptr` / `cgra_set_write_ptr`
2. `func()` — runs the CPU reference implementation
3. `check()` — validates CGRA `output[]` against CPU results, returns number of mismatches

---

## What `kernel_test` Measures

For each kernel, `ITERATIONS_PER_KERNEL` (default: 10) iterations are run. Each iteration records:

| Metric | What it counts |
|--------|---------------|
| `sw` | CPU cycles for the reference `func()` |
| `cgra` | CPU cycles from `cgra_set_kernel()` to interrupt return (wall time as seen by CPU) |
| `conf` | Bitstream configuration time (extracted from the first iteration) |
| `load` | `cgra_cmem_init()` time — only counted if `ANALYZE_EVERYTHING=1` |
| `dead` | Measurement overhead (a do-nothing interval timed the same way; subtracted from all other values) |

`CGRA_ACCESS_FLAT_COST_CYCLES = 80` is a fixed overhead subtracted from `cgra` time to remove the cost of the interrupt-entry path; this value was measured on QuestaSim and should not be changed unless you retarget to a different simulator or clock speed.

**Output columns printed by `kcom_printKernelStats`:**

```
<kernel_name>
  sw    avg=<N> stdev=<M>   ← CPU cycles for reference func()      (RISC-V mcycle)
  cgra  avg=<N> stdev=<M>   ← launch → completion interrupt         (RISC-V mcycle)
  conf  avg=<N>             ← run[0].cgra - run[i].cgra, i.e. the extra time the
                              first iteration took (bitstream load), applied to all
  repo  avg=<N> stdev=<M>   ← cols_max.cyc_act + cols_max.cyc_stl   (CGRA counters)
```

`sw` and `cgra` are both wall-clock from the RISC-V cycle CSR, so `sw / cgra` is
an end-to-end comparison that includes the offload overhead. That is the pair to
trust.

**`repo` is not `cgra - conf`.** It is the sum of the two CGRA hardware
counters (`kernels_common.c:227`), and those counters are **not disjoint** — the
active counter is enabled on `col_status | acc_req` and `col_status` is held from
`acc_ack` to `acc_end`, so it stays set through stall cycles, which the stall
counter also counts. Adding them double-counts every stalled cycle. The same file
computes `cyc_ratio = cyc_stl / cyc_act` ten lines later, which only makes sense
under the opposite (nested) reading — upstream is inconsistent with itself.

The measured numbers show it: strsearch on 4x4 reports `CGRA 338` but
`REPO 567`. The CGRA cannot be busy for more cycles than the CPU measured for the
whole launch-to-interrupt window, so `repo` overstates. Elapsed CGRA cycles is
`cyc_act` alone; `cyc_stl` is the stalled subset of it. See
`util/cgra_gen.py`, which had the same bug and no longer does.

---

## Compile-time Flags

All flags are in `kernels_common.h`:

| Flag | Default | Effect when 1 |
|------|---------|---------------|
| `EXECUTE_SOFTWARE` | 1 | Run `func()` and measure `sw` cycles |
| `MEASUREMENTS` | 1 | Record and print cycle counts |
| `PERFORM_RES_CHECK` | 1 | Call `check()` and print error count |
| `ANALYZE_EVERYTHING` | 1 | Also time bitstream load (`kcom_load`) |
| `PRINT_ITERATION_VALUES` | 0 | Print per-iteration values (verbose) |
| `PRINT_KERNEL_STATS` | 0 | Print column active/stall cycles from CGRA perf counters |
| `PRINT_COLUMN_STATS` | 0 | Print per-column active/stall cycles |
| `MEASURE_DEVIATION` | 1 | Compute and print standard deviation |
| `REPEAT_FIRST_INPUT` | 1 | Use the same random seed for the first two iterations (for reproducible VCD snapshots) |
| `ITERATIONS_PER_KERNEL` | 10 | Number of iterations per kernel |

---

## CGRA Performance Counters

The CGRA hardware has built-in performance counters readable via the driver:

```c
// Enable before launching
cgra_perf_cnt_enable(&cgra, true);
cgra_perf_cnt_reset(&cgra);

// ... run kernel ...

// Read after interrupt
uint32_t total_kernels = cgra_perf_cnt_get_kernel(&cgra);
uint32_t col0_active   = cgra_perf_cnt_get_col_active(&cgra, 0);
uint32_t col0_stall    = cgra_perf_cnt_get_col_stall(&cgra,  0);
```

| Counter | What it counts |
|---------|---------------|
| `CGRA_PERF_CNT_TOTAL_KERNELS` | Number of kernels completed since last reset |
| `CGRA_PERF_CNT_COL_n_ACTIVE_CYCLES` | Clock cycles a column was in CONF or EXEC state |
| `CGRA_PERF_CNT_COL_n_STALL_CYCLES` | Clock cycles a column was stalled (LWD/SWD bus wait or SMUL) |

**Active cycles** = configuration phase + execution phase (including stalls).  
**Stall cycles** = subset of active cycles during which the column PC was frozen.

The effective utilisation of a column is `(active - stall) / active`. A high stall ratio on a LWD-heavy kernel indicates the bottleneck is memory bandwidth, not compute.

Register offsets (from `cgra_regs.h`, base = `CGRA_PERIPH_START_ADDRESS`):

| Register | Offset |
|----------|--------|
| `CGRA_PERF_CNT_ENABLE` | `0x28` |
| `CGRA_PERF_CNT_RESET` | `0x2c` |
| `CGRA_PERF_CNT_TOTAL_KERNELS` | `0x30` |
| `CGRA_PERF_CNT_COL_0_ACTIVE_CYCLES` | `0x34` |
| `CGRA_PERF_CNT_COL_0_STALL_CYCLES` | `0x38` |
| `CGRA_PERF_CNT_COL_1_ACTIVE_CYCLES` | `0x3c` |
| … (stride `0x8` per column) | |

---

## Adding a New Kernel to `kernel_test`

### Step 1 — Create the kernel directory

```
sw/applications/kernel_test/kernels/mykern/
    mykern.h      ← declares extern kcom_kernel_t mykern_kernel;
    mykern.c      ← defines imem[], kmem[], input[], output[], config(), func(), check()
    function.h    ← optional: pure C reference implementation
```

### Step 2 — Implement `mykern.c`

```c
#include "../../kernels_common/kernels_common.h"
#include "mykern.h"

static uint32_t   my_imem[CGRA_CMEM_TOT_DEPTH];
static uint32_t   my_kmem[CGRA_KMEM_DEPTH];
static int32_t    my_input [N_IN]  __attribute__((aligned(4)));
static int32_t    my_output[N_OUT] __attribute__((aligned(4)));
static int32_t    ref_output[N_OUT];   // CPU reference result

static cgra_t     cgra;

static void my_config(void) {
    // fill my_input[] with test data
    for (int i = 0; i < N_IN; i++) my_input[i] = kcom_getRand();

    // set CGRA pointers (one pair per active column)
    cgra_set_read_ptr (&cgra, (uint32_t)my_input,  0);
    cgra_set_write_ptr(&cgra, (uint32_t)my_output, 0);
}

static void my_func(void) {
    // CPU reference: compute ref_output[] from my_input[]
    for (int i = 0; i < N_OUT; i++) ref_output[i] = /* ... */;
}

static uint32_t my_check(void) {
    uint32_t errors = 0;
    for (int i = 0; i < N_OUT; i++)
        if (my_output[i] != ref_output[i]) errors++;
    return errors;
}

kcom_kernel_t mykern_kernel = {
    .kmem   = my_kmem,
    .imem   = my_imem,
    .input  = my_input,
    .output = my_output,
    .col_n  = 1,
    .in_n   = N_IN,
    .out_n  = N_OUT,
    .config = my_config,
    .func   = my_func,
    .check  = my_check,
    .name   = "mykern",
};
```

`kcom_load()` (called by the harness before the iteration loop) calls `cgra_cmem_init(kernel->imem, kernel->kmem)`. Fill `my_imem[]` and `my_kmem[]` in a separate `init` function or at compile time; a good place is to call `cgra.base_addr` init and `memset` + bitstream fill inside `mykern.c`'s own init block, or do it lazily in `config()` on first call.

### Step 3 — Register in `main.c`

```c
// sw/applications/kernel_test/main.c
#include "kernels/mykern/mykern.h"

static kcom_kernel_t *kernels[] = {
    &mykern_kernel,
    // &conv_kernel,
    // ...
};
```

Uncomment/comment entries to select which kernels run in a given build.

---

## Interpreting the Output

Example output from a strsearch kernel run:

```
 strsearch
  sw    avg=12340  stdev=45
  cgra  avg= 4210  stdev=12
  conf  avg=  890
  repo  avg= 3320  stdev=11
  errors: 0
```

- **Speedup** = `sw.avg / repo.avg` ≈ 3.7× (excluding configuration cost)
- **With config amortised**: if you call this kernel 100 times, the effective CGRA cost per call is `repo.avg + conf.avg/100` ≈ 3329 cycles → 3.7× speedup holds.
- **errors: 0** — CGRA output matches CPU reference on all 10 iterations.

A high `stdev` relative to `avg` indicates interference from cache effects or interrupt latency jitter; run more iterations with `ITERATIONS_PER_KERNEL` to smooth it out.

---

## Grid sizes

Each kernel's `.c` is generated by `utils/heeptest_gen.py <kernel_path> <CxR>` from
the per-dimension folders (`out.sat`, `io.json`, `bitstreams`). The generated file
carries one `#if CGRA_N_COLS == N` branch per dimension it was generated for, and
selects at compile time from `cgra.h` — so no regeneration is needed to switch
grids, only a rebuild.

| Dimension | Available |
|---|---|
| 2x2 | bitcount, gsm, reversebits, sha, sqrt, strsearch |
| 3x3 | all of the above + sha2 |
| 4x4 | all of the above (branches present in the generated `.c` even where the source folder is gone) |
| 5x5 | **none** — no kernel has a 5-column branch |

On a 5-column build every branch evaluates false, so the kernel descriptors
`main.c` references do not exist and the link fails. Supporting 5x5 means running
SAT-MapIt for those kernels at `-x 5 -y 5` and regenerating.

A mapping is only valid on the grid it was solved for: the inter-column mesh
wraps `N_COL-1 → 0`, so a 3x3 mapping expects col2's `RCR` to reach col0, which on
a 4-column build physically reaches col3. Nothing detects this — see
`docs/cgra_grid_configs.md`.

## Which kernels are enabled

`main.c` ships with **only `strs_kernel` uncommented**; the other seven are
commented out. A default `make run-verilator PROJECT=kernel_test` therefore
exercises strsearch alone. Uncomment entries in `kernels[]` to add more — but note
that the harness runs them sequentially in one binary, so a kernel that never
raises its completion interrupt blocks every kernel after it. To survey them,
build and run one at a time.

## GCC 14 — kernel_test does not currently build

Measured 2026-07-29 with the repo's `riscv32-corev-elf-gcc` 14.1.0. These are
**unfixed** — the files are upstream (EPFL) and were deliberately left untouched.
Anything below must be patched before `make app PROJECT=kernel_test` succeeds.

| Location | Problem |
|---|---|
| `main.c` | `void main()` with `return 0`; `&run` passed where `kcom_run_t*` is expected (3 sites) |
| generated `kernels/*/*.c` | `.input = cgra_input` — 2-D array assigned to `kcom_io_t` (`int32_t*`), ~40 sites across 9 files |
| generated `kernels/*/*.c` | `cgra_input[c][i] = <buffer>` — address stored in an `int32_t` with no cast |
| `kernels/sha/function.h` | declared `uint32_t`, body does `return (int32_t*)W` |
| `kernels_common/kernels_common.c` | `pinInit()` / `timerInit()` called at line 443, defined at 498/512, no prototype |

Two of these originate in `utils/source.c.tpl` and `utils/heeptest_gen.py`, so
fixing only the generated `.c` files gets undone by the next regeneration.

**Non-4-column builds have an extra blocker.** `kernels/sabs/sabs.c` ends in
`#error "sabs kernel is only implemented for CGRA_N_COLS == 4"`. `sabs` is not
referenced from `main.c`, but CMake compiles every `.c` under the application
directory, so that `#error` makes the whole of `kernel_test` unbuildable on 2x2
and 3x3.

## Measured results (Verilator, 4x4, with the above patched locally)

10 iterations per kernel, cycle counts are the reported means.

| Kernel | Result | CPU cy | CGRA cy | Speedup |
|---|---|---|---|---|
| strsearch | pass | 763 | 338 | 2.26x |
| bitcount | pass | 111 | 57 | 1.95x |
| sha | pass | 1519 | 791 | 1.92x |
| gsm | pass | 610 | 360 | 1.69x |
| sqrt | **fail**, 10/10 iterations | 197 | 33 | — |
| sha2 | **fail**, 50 errors | 544 | 247 | — |
| reversebits | **hangs**, killed at 200 s | — | — | — |

4 of 7. On 3x3, reversebits passes (305 -> 107 cy); the rest of that sweep was
not completed.

On sqrt, CGRA time is 33 cycles with a standard deviation of 0.0 — constant
across all 10 iterations, for a kernel whose iteration count depends on its
input. Constant time on a data-dependent loop means the loop is not iterating.
