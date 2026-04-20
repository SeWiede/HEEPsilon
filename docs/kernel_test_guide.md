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
  sw    avg=<N> stdev=<M>   ← CPU cycles for reference func()
  cgra  avg=<N> stdev=<M>   ← net CGRA cycles (execution + config overhead)
  conf  avg=<N>             ← bitstream load time (first iter, applied to all)
  repo  avg=<N> stdev=<M>   ← cgra - conf (pure execution cycles)
```

A lower `repo` vs. `sw` means a real speedup. `conf` amortises over multiple calls if you call the same kernel repeatedly.

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
