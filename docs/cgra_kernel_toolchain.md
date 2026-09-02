# CGRA Kernel Toolchain

This document describes the Python tools in `util/` that automate the pipeline
from a C loop to a runnable HEEPsilon SW application.

The toolchain is split into a **front end** and a **shared tail**. A front end's
only job is to produce LLVM IR whose target loop carries
`!{!"llvm.loop.cgra.acc"}`; `util/cgra_tail.py` then takes it the rest of the way
(`opt -passes=cgra-extract` → SAT-MapIt mapper → `satmapit_parse.py` →
`cgra_gen.py`). Two front ends exist:

| Front end | Input | How the loop is tagged |
|---|---|---|
| `util/cgra_satmap.py` | C with `#pragma cgra acc` | clang emits the metadata |
| `util/cgra_glsl.py` | GLSL compute shader | SPIR-V + MLIR, tag injected after canonicalisation — see `cgra_glsl_route.md` |

Everything below the `opt` stage is common to both, so a change there affects
both routes. `util/cgra_tail.py` is a module, not a CLI.

---

## Overview

```
C source file  (loop annotated with #pragma cgra acc)
        │
        │  python util/cgra_satmap.py kernel.c     ← util/cgra_satmap.py
        ▼
  sw/satmapit/instructions_<kernel>.py   (draft — manual review required)
        │
        │  python util/cgra_gen.py <spec.py> <app>  ← util/cgra_gen.py
        ▼
  sw/satmapit/<app>/main.c   (complete skeleton, ready to fill in)
        │
        │  make run-verilator PROJECT=<app> APP_DIR=satmapit
        ▼
  uart0.log
```

`sw/satmapit/` is gitignored and serves as the working area for all
intermediate and generated files.  Once an application is production-ready,
move it to `sw/applications/` and commit it.

If you already have a `cgra-code-acc1` file from a manual SAT-MapIt run,
use `util/satmapit_parse.py` directly (skipping `cgra_satmap.py`).

---

## util/cgra_satmap.py

Runs the full SAT-MapIt pipeline on a C source file and produces a draft
`instructions_*.py` kernel spec in one command.  Internally it invokes
clang, opt, and the SAT-MapIt mapper, then calls `satmapit_parse.py`.

```bash
python util/cgra_satmap.py <source.c> [--name instructions_<kernel>] \
    [--n-row 4] [--n-col 4] [--out-dir sw/satmapit]
```

| Option | Default | Description |
|---|---|---|
| `--name` | `instructions_<basename>` | Output filename stem |
| `--n-row` | `4` | SAT-MapIt `-x` value (HEEPsilon rows) |
| `--n-col` | `4` | SAT-MapIt `-y` value (HEEPsilon columns) |
| `--out-dir` | `sw/satmapit` | Where to write the draft spec |
| `--satmapit-dir` | `../SAT-MapIt` | Path to SAT-MapIt checkout |

The tool expects SAT-MapIt to be checked out next to HEEPsilon
(`../SAT-MapIt` by default).  Override with `--satmapit-dir` or the
`SATMAPIT_DIR` environment variable.

**Example:**
```bash
python util/cgra_satmap.py \
    /path/to/SAT-MapIt/benchmarks/vec_sum/vec_sum.c \
    --name instructions_vec_sum
# → sw/satmapit/instructions_vec_sum.py
```

---

## util/satmapit_parse.py

Translates an existing SAT-MapIt `cgra-code-acc1` output file into a draft
`instructions_*.py` kernel spec.  Use this when you already have the
SAT-MapIt output and want to skip the mapping step.

```bash
python util/satmapit_parse.py <cgra-code-acc1> \
    [--name instructions_<kernel>] \
    [--n-row 4] [--n-col 4] \
    [--out-dir sw/satmapit] \
    [--stdout]
```

### SAT-MapIt → HEEPsilon mapping

SAT-MapIt and HEEPsilon use different execution models:

| | SAT-MapIt | HEEPsilon |
|---|---|---|
| Basic unit | PE (Processing Element) | RC (Reconfigurable Cell) |
| Each RC | executes its own instruction stream | executes its own instruction stream |
| Shared | nothing | logical PC counter (all active columns step together) |
| Memory | LWI (indexed load, address computed by SMUL) | LWD (streaming, pointer auto-increments) |
| Output routing | ROUT → neighbour | ROUT (registered) + explicit register file R0–R3 |

With `-x 4 -y 4` (default), SAT-MapIt produces a 16-PE grid which maps
directly to the 4×4 HEEPsilon CGRA:

```
SAT-MapIt col j (x-dir)  →  HEEPsilon row j
SAT-MapIt row k (y-dir)  →  HEEPsilon column k
SAT-MapIt time T          →  HEEPsilon PC T
SAT-MapIt RCL/RCR         →  HEEPsilon RCT/RCB  (x-dir → vertical mesh)
SAT-MapIt RCT/RCB         →  HEEPsilon RCL/RCR  (y-dir → horizontal mesh)
```

With `-x 4 -y 1` (single-column kernel), only one HEEPsilon column is used:

```
SAT-MapIt column j  →  HEEPsilon row j  (col 0 only, col_mask=0x1)
SAT-MapIt RCR       →  HEEPsilon RCB
SAT-MapIt RCL       →  HEEPsilon RCT
```

Single-column kernels are simpler to write and debug; multi-column kernels
offer more parallelism but require extra care (see review checklist).

---

## util/cgra_gen.py

Encodes a finished `instructions_*.py` spec into a complete
`sw/satmapit/<name>/main.c`.  The generated file contains:

- Sparse `cgra_cmem[]` and `cgra_kmem[]` C arrays with the encoded bitstream
- Standard CGRA boilerplate (PLIC setup, interrupt handler, `cgra_cmem_init`)
- Auto-sized buffer declarations and `cgra_set_read_ptr`/`cgra_set_write_ptr` calls
- When `--ref-src` is given: the extracted reference function, auto-generated test data, and PASS/FAIL verification

```bash
# Single kernel, with automatic reference/test generation
python util/cgra_gen.py sw/satmapit/instructions_<kernel>.py <app_name> \
    --ref-src /path/to/original_kernel.c

# Without --ref-src: CMEM/KMEM + boilerplate only; fill test data manually
python util/cgra_gen.py sw/satmapit/instructions_<kernel>.py <app_name>

# Multiple kernels packed into the same CMEM
python util/cgra_gen.py <spec1.py> <spec2.py> ... <app_name> --ref-src ...

# Output to sw/applications/ when the app is ready to commit
python util/cgra_gen.py <spec.py> <app_name> --out-dir sw/applications
```

`--ref-src` expects a C file containing the function with `#pragma cgra acc`.  The
generator strips the pragma, renames the function `<name>_ref`, embeds it in
`main.c`, and auto-generates test data + PASS/FAIL comparison.  What it can and
cannot auto-generate is described in the [Automation scope](#automation-scope) section below.

`cgra_satmap.py` passes `--ref-src` automatically — no manual step needed when
using the full pipeline.

---

## Review checklist

After `cgra_satmap.py` or `satmapit_parse.py` generates a draft, review
and fix the following before running `cgra_gen.py`:

1. **Assign dest registers.** SAT-MapIt uses ROUT for all outputs; HEEPsilon
   needs explicit `R0`–`R3` targets.  Replace `-` dest markers wherever a
   value needs to persist beyond the next cycle.

2. **Convert LWI → LWD.** SAT-MapIt generates `LWI` (indexed load) with
   address arithmetic (typically `SMUL index, 4` + `LWI`).  Replace with
   streaming `LWD` and arrange the input buffer so values arrive in order.

3. **Remove SMUL address arithmetic.** `SMUL` has a 3-cycle stall.  If the
   only reason for SMUL was to compute a byte offset for LWI, removing LWI
   removes it too.

4. **Handle phi (loop-carried) values.** Assign them to a register (`R0`–`R3`),
   initialise before the loop (PC 0 prologue), and update each iteration.

   **Important:** `satmapit_parse.py` converts most phi nodes to NOP under the
   assumption that ROUT retention carries loop values.  This is correct when the
   downstream instruction reads `SELF` (own ROUT) — but **wrong** when it reads a
   named register (`R0`–`R3`).  A phi of the form `SADD R0, ROUT, ZERO` that writes
   to `R0` is a *recurrence carrier*: it copies the previous cycle's ROUT into R0 for
   the next iteration.  The parser now keeps these, but if a kernel loop exits after
   exactly one iteration with a count of 1 regardless of input, the likely cause is a
   NOP'd recurrence-carry phi leaving a named register frozen at 0.
   Verify in `instructions_*.py` that any phi writing to `R0`–`R3` is present, not NOP'd.

5. **One-hot branch constraint.** Only one row per column may issue a branch
   at any given PC.  All other rows must have `NOP` at that PC.

6. **Branch column placement (FPGA timing).** In multi-column kernels
   (`col_mask > 0x1`), BNE/BEQ must be in the **lowest-indexed active column**
   (col=0).  SAT-MapIt does not enforce this.  Placing the branch in a higher
   column passes Verilator but fails on FPGA: the branch-propagation path
   violates setup time and the loop executes exactly one body iteration.
   Move loop control (counter + branch) to col=0 manually if needed.

7. **SELF timing.** `SELF` reads the registered output from the previous cycle.
   It works correctly in all rows and columns — use it freely for loop-carried
   values.  If you need a value from more than one cycle back, save it to a
   register instead.

8. **SWD source.** `SWD` reads from `muxA`.  The tool translates SAT-MapIt's
   `SWD ROUT` to `muxA=SELF`.  Verify that is the value you want to store.

9. **Branch target.** PC targets are relative to `start_add` (0-based kernel
   start), not absolute CMEM addresses.  Verify the immediate matches the
   intended loop-back PC.

---

## Automation scope

`cgra_gen.py` with `--ref-src` auto-generates the following for **simple kernels**
(where every data column performs at most one LWD per kernel iteration):

| Item | Auto | Notes |
|---|---|---|
| CMEM/KMEM bitstream | ✓ always | |
| Buffer declarations | ✓ always | Sizes from CMEM scan + section counts |
| `cgra_set_read_ptr` / `cgra_set_write_ptr` | ✓ always | |
| Reference function `<name>_ref()` | ✓ with `--ref-src` | Full function body, pragma stripped |
| Test data fill | ✓ with `--ref-src` | Data cols: `(_i+1)*prime`; scalar col[0]=0, col[1+]=multiples of 7 |
| PASS/FAIL comparison | ✓ simple kernels | |
| PASS/FAIL comparison | ✗ complex kernels | Same source array fans out to multiple streams at different offsets (e.g. SHA-style) — write `verify.h` manually; see `sw/satmapit/cgra_sha/verify.h` as an example |

For **complex kernels**, `cgra_gen.py` emits a print-only stub and a comment
pointing to `cgra_sha/verify.h`.

---

## Known caveats and limitations

### 1. Phi nodes that write to named registers are recurrence carriers (FIXED in satmapit_parse.py)

`satmapit_parse.py` previously NOP'd ALL phi nodes whose `srcA` was a register or
ROUT.  This is safe when the next instruction reads `SELF` (the phi's only effect
is ROUT retention, which happens anyway).  It is **wrong** when the next instruction
reads `R0`–`R3` by name: the phi was writing the loop-carried value into that
register, and NOP-ing it freezes the register at its initial value (0).

**Symptom:** the loop always runs exactly one iteration; the output count is always 1
regardless of input.  Verilator and FPGA show the same wrong result.

**Fix (already applied):** `satmapit_parse.py` now keeps phis that write to `R0`–`R3`.
When reviewing a generated `instructions_*.py`, verify that any `SADD Rn, ROUT, ZERO`
phi at the kernel entry PC is present, not NOP'd.

---

### 2. BNE/BEQ in column > 0 fails on FPGA (1-iteration symptom)

In multi-column kernels (`col_mask > 0x1`), placing BNE or BEQ in any column other
than column 0 passes Verilator but executes only one loop iteration on FPGA.
The branch signal does not propagate in time to restart the pipeline.

**Fix:** Either (a) redesign as a **single-column** kernel (col_mask=0x1), which
avoids the issue entirely since the only column is trivially col 0, or (b) keep the
multi-column layout but move the branch PE to row *, col **0** manually.

See the `cgra_bit_count` redesign in `sw/satmapit/instructions_bit_count.py` for
an example of option (a).  Review checklist item 6 has further detail.

---

### 3. Multiple LWDs in the same column at the same T consume sequential stream elements

If two rows in the same column both issue `LWD` at the same T (e.g. T=0 init),
the column's stream pointer advances **once per LWD PE**, not once per T step.
The two rows receive **different** values: the first in row order gets element [0],
the second gets element [1].

**Impact on buffer sizing:** `cgra_gen.py`'s `infer_io_layout()` previously counted
only unique T values per column, giving buffer size 1 when two LWDs at T=0 needed
buffer size 2.  This is now fixed (per-PE counting), but worth knowing if you size
buffers by hand.

**Typical layout for a scalar input column with two init LWDs:**

```c
col_in[0] = 0;   // BNE comparison constant — must be 0
col_in[1] = x;   // actual data input
```

---

### 4. The CGRA kernel maps only the `#pragma cgra acc` region, not surrounding guards

If the C source has a guard before the annotated loop (e.g. `if (x == 0) return 0;`),
that guard is **not** part of the CGRA kernel.  The reference function `<name>_ref()`
extracted by `cgra_gen.py` includes the full function body (including the guard), so
for inputs that trigger the guard the reference and CGRA will disagree.

**Implication for test data:** always use non-degenerate inputs that exercise the
actual loop body.  For a bit-count kernel, use `x ≠ 0`; for a loop guarded on
`N > 0`, use `N ≥ 1`.  The auto-generated scalar fill uses `col_in[1] = 7` (a
small non-zero value with 3 set bits) as a default.

---

### 5. SMUL is only removed for address-arithmetic PEs (not data computation)

`satmapit_parse.py` originally removed ALL `SMUL` instructions, assuming they were only
used for LWI address arithmetic (`index * 4` pointer offsets).  For kernels like `isqrt32`
that use `SMUL` for data computation (`temp * temp`), this destroyed the result.

**Current behaviour (fixed 2026-07-17):** `SMUL` is removed only for PEs that are part of
an `LWI` address chain (`lwi_pes`).  If the kernel has no `LWI` instructions at all (all
loads are already `LWD`), `SMUL` is preserved.

**SMUL has a 3-cycle column stall.**  SAT-MapIt schedules the consuming instruction (e.g.
`SSUB`) one PC after `SMUL`, relying on the stall to make the result available.  The entire
active column freezes during those 3 cycles, so multi-column kernels stay in sync.

---

### 6. BSFA/BZFA argument order and flag source

`BSFA` and `BZFA` are conditional-select instructions.  SAT-MapIt emits them as:

```
BSFA DEST, mux_b_if_false, mux_a_if_true, flag_src_direction
```

This is the **opposite** of HEEPsilon's encoding (where `mux_a` is the "if sign-flag" case).
`satmapit_parse.py` now swaps the two source operands for `BSFA`/`BZFA` and maps the
4th argument to the `flag_s` (muxF) field.  If the flag source is a mesh direction it
is remapped through the same SAT-MapIt→HEEPsilon direction table.

**Symptom of the bug:** the conditional select always outputs the wrong branch; because
the flag source was also lost (defaulted to `SELF`), the flag came from the wrong PE.

---

### 7. Phi loop-carry source (opB) vs. init source (opA)

SAT-MapIt phi nodes encode two sources: `opA` (the init/prologue value, typically loaded
by `LWD` in the init section) and `opB` (the loop-carried value produced by the previous
kernel iteration, e.g. the shifted mask from `SRT`).

`satmapit_parse.py` used to generate the phi instruction using only `opA` (the init
source), so on every iteration the loop variable was reset to the initial value instead
of advancing.

**Symptom:** loop variable never advances → branch condition always true → simulation
does not terminate.

**Current fix:** when `opA ≠ opB` and both are named registers (`R0`–`R3`), the parser
applies a "phi loop-carry redirect":
1. The `LWD` in the init section that previously loaded into `opA` is rewritten to load
   into `opB` instead (so `opB` holds the correct initial value after init).
2. The phi instruction is rewritten from `SADD Rn, opA, ZERO` to `SADD Rn, opB, ZERO`.

After this redirect the phi always reads the loop-carry register, which holds the init
value on the first iteration and the updated value on all subsequent ones.

---

### 8. BSFA and exact equality (perfect-square inputs for isqrt)

`BSFA` triggers on the **sign flag** only (bit 31 of the selected ROUT).  The condition
`temp² ≤ in_ptr` is implemented as `SSUB = temp² − in_ptr`.  When `temp² = in_ptr`
exactly, `SSUB = 0`; the sign flag is 0 (non-negative), so `BSFA` treats this as
"do not update result."

**Impact:** for isqrt of a perfect square (e.g. 4, 9, 25, 49), the CGRA returns
`floor(sqrt(N)) − 1` in one of the intermediate bit positions, giving a result one less
than correct.  Use non-perfect-square inputs for testing (e.g. `50` for `isqrt = 7`).

---

### 9. Data-dependent loop bounds are not supported

The CGRA executes a fixed schedule determined at mapping time.  Loops whose
iteration count depends on the data value (e.g. `do { } while (x != 0)`) can be
mapped, but only if the bound is loaded from the stream at init time and used as
the BNE comparison target — the scheduler must know the maximum possible iteration
count at compile time to size the kernel correctly.  Mapping arbitrary data-dependent
loops is not supported by SAT-MapIt in its current form.

---

### 10. Two-array kernels: reference-arg mapping (FIXED) and a residual +1 (OPEN)

`cgra_gen.py`'s `--ref-src` reference-call generation used to map **every** pointer
parameter to the same first data column (`data_cols[0]`), instead of consuming
`data_cols` in order the way it already did for multiple scalar columns.  For a
kernel with two array parameters (e.g. `acc += a_arr[i] * b_arr[i]`), both arguments
of the generated reference call pointed at the same buffer — the PASS/FAIL check
silently compared CGRA output against a reference computing `a[i]*a[i]`, not
`a[i]*b[i]`.  **Fixed**: `_build_ref_args()` / `gen_test_harness()` now consume
`data_cols` one at a time per pointer parameter, matching pointer arguments to
columns in appearance order.

Known open issue found while fixing the above (a two-array dot-product kernel): once the
mapping was corrected, the CGRA result was `expected + 1` in every sweep trial,
regardless of N or the randomized data — a constant, data-independent offset, not
a stream-alignment issue (which would scale with the data). Root cause identified
(not yet fixed): a residual `LWI`-address-arithmetic `SMUL` survives on both array
columns (`satmapit_parse.py`'s `lwi_pes` chain detection, caveat #5, apparently
doesn't recognize both of two independent array-index chains in the same kernel).
On one column it's genuinely dead (never read again — wasted cycles only). On the
other, its result (`dest="-"`, never saved to a register) is consumed by the very
next instruction via `SELF`/`own_res` — which is exactly the documented hardware
gotcha in `docs/cgra_isa.md` ("Timing notes and known gotchas" → `own_res`
latency): `own_res` right after an SMUL stall is unreliable; the fix is
`reg_we=1` + read the named register instead of `SELF`. Distinct from sqrt's
perfect-square issue (caveat #8) — checked, sqrt's SMUL consumer isn't in this
same-PE-immediately-after-stall shape.

---

## Full workflow example

```bash
# 1. Run SAT-MapIt on a C source with #pragma cgra acc
#    cgra_satmap.py runs the full pipeline and generates main.c automatically.
python util/cgra_satmap.py \
    /path/to/my_kernel.c \
    --app cgra_my_kernel
# → sw/satmapit/instructions_my_kernel.py  (draft spec)
# → sw/satmapit/cgra_my_kernel/main.c      (complete app, PASS/FAIL included)

# 2. Review the generated instructions_*.py (see review checklist)
#    Pay attention to: phi nodes writing R0-R3, BNE column placement,
#    and whether test data in main.c is non-degenerate.
$EDITOR sw/satmapit/instructions_my_kernel.py

# 3. If you changed instructions_*.py, regenerate main.c
python util/cgra_gen.py \
    sw/satmapit/instructions_my_kernel.py cgra_my_kernel \
    --ref-src /path/to/my_kernel.c

# 4. Simulate with Verilator
python run.py --simulator verilator --tests cgra_my_kernel

# 5. Run on FPGA
python run.py --board zcu104 --tests cgra_my_kernel

# 6. When production-ready, move to sw/applications/ and commit
python util/cgra_gen.py \
    sw/satmapit/instructions_my_kernel.py cgra_my_kernel \
    --ref-src /path/to/my_kernel.c \
    --out-dir sw/applications
```
