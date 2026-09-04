# GLSL Compute Shader → CGRA (`util/cgra_glsl.py`)

This document describes the second front end to the CGRA kernel toolchain: a
Vulkan/GLSL compute shader instead of C annotated with `#pragma cgra acc`. It
covers the mechanism both front ends share, the pipeline stages and why each is
needed, the constraints on a shader, and the failure modes.

Status: working proof of concept, verified on Verilator. Restricted to integer,
single-invocation kernels with one counted innermost loop — see
[Limits](#limits).

Companion: `cgra_kernel_toolchain.md` (the C route and the shared tail).

---

## Overview

Both routes converge on the same `opt` pass. Everything after `cgra-extract` is
the existing, verified tail — the CGRA never sees which front end produced the
IR.

```
 C source                                GLSL compute shader
 (#pragma cgra acc)                      (.comp.glsl)
        │                                        │
        │ clang                                  │ glslc → spirv-opt
        │                                        │ mlir-translate → mlir-opt
        │                                        │ mlir-translate
        ▼                                        ▼
   LLVM IR                                   LLVM IR
   loop carries                              canonicalised, then tagged
   !{!"llvm.loop.cgra.acc"}                  !{!"llvm.loop.cgra.acc"}
        │                                        │
        └──────────────┬─────────────────────────┘
                       ▼
        opt -passes=cgra-extract   →  acc1/  (DFG)
                       ▼
        SAT-MapIt mapper           →  cgra-code-acc1  (schedule)
                       ▼
        util/satmapit_parse.py     →  instructions_*.py
                       ▼
        util/cgra_gen.py           →  sw/satmapit/<app>/
                       ▼
        python run.py --simulator verilator --tests <app>
```

`util/cgra_satmap.py` drives the left branch, `util/cgra_glsl.py` the right.
Everything from `cgra-extract` down is `util/cgra_tail.py`, imported by both —
the same code, not a copy. A front end produces a tagged `.ll` and calls
`tail.drive_tail()`; that split is what spike-0 established, and it is why the
two routes cannot drift apart.

---

## The loop tag — what the two routes share

`#pragma cgra acc` compiles to exactly one thing: an MDString
`"llvm.loop.cgra.acc"` among the operands of the loop's `!llvm.loop` metadata
node. No function attribute, no marker intrinsic, no custom metadata kind.

- Producer (C route): `clang/lib/CodeGen/CGLoopInfo.cpp:436` in the SAT-MapIt
  LLVM fork.
- Consumer (both routes): `CGRAExtract.cpp:20` `hasPragmaCGRAAcc()`, called from
  `CGRAExtractPass::run()` — innermost loops only (`getSubLoops().empty()`).

Minimal well-formed form:

```llvm
  br i1 %done, label %exit, label %loop, !llvm.loop !0
!0 = distinct !{!0, !1}
!1 = !{!"llvm.loop.cgra.acc"}
```

Contract, all four parts required (each verified by a negative test):

| Requirement | Why |
|---|---|
| Attached as `!llvm.loop` on the **latch terminator** | `Loop::getLoopID()` looks only there |
| Node's operand 0 is the node itself (`distinct !{!0, …}`) | `getLoopID()` returns `nullptr` otherwise |
| Tag at operand index **≥ 1** | the scan in `hasPragmaCGRAAcc` starts at `i = 1` |
| Exact string `"llvm.loop.cgra.acc"` | `StringRef::compare` |

Nothing clang-specific is needed — no target triple, datalayout, `!tbaa`,
attributes or debug info. `cgra_glsl.py` synthesises this metadata itself
(`_tag_loop()`), because MLIR emits no loop metadata at all.

---

## Usage

```sh
conda activate core-v-mini-mcu

python util/cgra_glsl.py vec_sum.comp.glsl --app cgra_glsl_vec_sum \
    --ref-src ../SAT-MapIt/benchmarks/vec_sum/vec_sum.c

python run.py --simulator verilator --tests cgra_glsl_vec_sum
```

Flags mirror `cgra_satmap.py` (`--app`, `--name`, `--n-row`/`--n-col`,
`--out-dir`, `--satmapit-dir`; unknown args forwarded to `cgra_gen.py`), plus:

| Flag | Purpose |
|---|---|
| `--ref-src <file.c>` | Host-side verification oracle — see [The oracle](#the-oracle-ref-src) |
| `--mlir-dir <path>` | LLVM 20 install with `mlir-opt`/`mlir-translate` (default `/usr/lib/llvm-20`) |
| `--keep-ir <dir>` | Copy every intermediate (`.spv`, `.mlir`, `.ll`) out for inspection |

A pre-built `.spv` may be passed instead of `.glsl` (glslc is skipped, and the
shader-source checks are skipped with it).

### Prerequisites

```sh
sudo apt install mlir-20-tools      # provides mlir-opt, mlir-translate
```

Version 20 matches the SAT-MapIt LLVM fork. The package does **not** register
`libMLIR.so.20.1` with ldconfig, so the tools fail to start with a shared-library
error; `cgra_glsl.py` sets `LD_LIBRARY_PATH` itself (`_mlir_env()`). `glslc` and
`spirv-opt` come from the Vulkan SDK on `PATH`.

Everything is checked before any work starts, each with the fix for that specific
failure:

| Checked | If missing |
|---|---|
| SAT-MapIt dir, `opt`, `mapper`, venv python | `run setup.sh in the SAT-MapIt repo, or pass --satmapit-dir` |
| `mlir-opt`, `mlir-translate` exist | `sudo apt install mlir-20-tools, or pass --mlir-dir` |
| `mlir-opt --version` actually **runs** | `sudo apt install libmlir-20 mlir-20-tools` |
| `glslc`, `spirv-opt` on PATH | `source the Vulkan SDK setup script` |
| `clang.cindex` importable (only with `--ref-src`) | `conda activate core-v-mini-mcu` |

Two of those are worth noting. The `mlir-opt --version` probe exists because the
binaries can be **present but unable to start** when `libmlir-20` is absent —
an existence check passes and the failure then surfaces two stages later as an
opaque error. And `clang.cindex` is checked because `cgra_gen.py` uses it to read
the `--ref-src` loop bound; without it the trip count is silently guessed and the
generated app can hang in simulation, so this fails fast instead.

---

## Pipeline stages

Each step exists because something downstream rejects the previous form.

| # | Step | Why |
|---|---|---|
| 1 | `glslc -fshader-stage=comp --target-env=vulkan1.1` | vulkan1.1 ⇒ SPIR-V 1.3 `Block` + `StorageBuffer`. SPIR-V 1.0's `BufferBlock` + `Uniform` is rejected by the MLIR converter. |
| 2 | `spirv-opt --strip-debug` | MLIR turns *named* SPIR-V structs into **identified** structs; `convertStructType()` only handles literal ones. glslang always names interface blocks. |
| 3 | `mlir-translate --deserialize-spirv` | SPIR-V binary → `spirv` dialect. Faithful and reliable; this stage has never been the problem. |
| 4 | `sed s/\bsi32\b/i32/g` | glslang's `int` is `OpTypeInt 32 1` → MLIR `si32`, but `llvm.icmp`/`llvm.add` require **signless** `i32`. See [the signedness rewrite](#the-signedness-rewrite). |
| 5 | `mlir-opt --convert-spirv-to-llvm` | `spirv` dialect → LLVM dialect. |
| 6 | unwrap nested module | The converter emits `module { module { … } }` and `--mlir-to-llvmir` translates only the outer one — **silently producing an empty `.ll`**. |
| 7 | `mlir-translate --mlir-to-llvmir` | LLVM dialect → textual LLVM IR. |
| 8 | add 32-bit datalayout | MLIR emits none, so LLVM assumes 64-bit pointers and `instcombine` injects `sext i32 to i64` into the loop body — an extra node the ISA has no case for. |
| 9 | flatten access-chain GEPs | See [the GEP operand budget](#the-gep-operand-budget). |
| 10 | canonicalise (`opt`) | See below — order is load-bearing. |
| 11 | inject the loop tag | MLIR emits no loop metadata; nothing to attach to otherwise. |

### The canonicalisation pipeline

A direct SPIR-V lowering is in "memory SSA" form (every `OpVariable` is an
alloca), keeps SPIR-V's four-block structured loop, and re-loads loop-invariant
values every iteration. `cgra-extract` accepts none of that.

```
sroa, instcombine, simplifycfg, loop-simplify, loop-rotate,
loop-mssa(licm), indvars, simplifycfg, instcombine
```

| Pass | Why |
|---|---|
| `sroa` | `OpVariable` allocas → phi nodes. The CGRA maps phis; it cannot map the load/store form. |
| `simplifycfg` | Collapse SPIR-V's header/cond/body/continue blocks. **Must precede `loop-rotate`** — SPIR-V's header is just `br label %cond`, not an exiting block, so until this runs rotation has nothing to do. |
| `loop-simplify` | Preheader and single latch, as `loop-rotate` requires. |
| `loop-rotate` | while → do-while, so the latch ends in a **conditional** branch. `cgra-extract` calls a bare `exit(0)` on an unconditional one (`CGRAExtract.cpp:865`). |
| `loop-mssa(licm)` | Hoist the trip count out of the loop body. Needs the `loop-mssa` wrapper or `opt` aborts with `LICM requires MemorySSA`. |
| `indvars` | **Loop-exit polarity** — see below. |

### Loop-exit polarity (`indvars`)

`cgra-extract` treats the branch instruction as a no-op ("Should be solved from
the icmp", `CGRAExtract.cpp:859`) and emits the **icmp's predicate** as the CGRA
branch. It never inspects which successor is the loop body, so it silently
assumes clang's convention:

| Front end | Exit test | Condition true means |
|---|---|---|
| clang `-O3` | `icmp eq %i.next, %N` | **exit** |
| glslang | `icmp slt %i.next, %N` | **loop** |

Opposite polarity. Without `indvars` the kernel extracts cleanly, schedules at a
valid `II`, builds, runs — and the loop exits almost immediately. Symptom:
`cgra_active` constant regardless of `N`, all sweep points wrong.

`indvars` performs linear-function-test-replacement, rewriting the exit test to
clang's exact form:

```llvm
  %exitcond.not = icmp eq i32 %i_n, %smax
  br i1 %exitcond.not, label %merge, label %latch, !llvm.loop !0
```

The `llvm.smax.i32` it introduces lands in the **preheader**, outside the loop,
so `cgra-extract` never walks it — important, since an intrinsic call inside the
loop is an unsupported-instruction failure.

### The GEP operand budget

`assignInstructionOperands()` disables instruction selection for the **entire
graph** if any instruction has more than 3 operands (`CGRAExtract.cpp:338`):

```
More than 3 operands for inst   %p = getelementptr { [1024 x i32] }, ptr @buf, i32 0, i32 0, i32 %i
Instruction Selection will be disabled!
```

Exit code 0, and `acc1/` is still written — but every node's opcode is `-1`,
surfacing much later as `UNDEF`. SPIR-V's `Block` struct wrapper makes
`OpAccessChain` lower to a 3-index GEP (4 LLVM operands). No stock LLVM pass
flattens it (`separate-const-offset-from-gep`, `gvn`, `slsr`,
`nary-reassociate`, `infer-address-spaces`, `sccp`, `aggressive-instcombine` all
tested) — LLVM deliberately preserves struct-typed GEPs for alias analysis. So
`_prep_for_cgra()` rewrites it textually; valid because member 0 sits at offset 0:

```llvm
- getelementptr { [1024 x i32] }, ptr @buf, i32 0, i32 0, i32 %i
+ getelementptr i32, ptr @buf, i32 %i
```

Budget is base pointer + at most 2 indices.

### The signedness rewrite

The load-bearing hack. glslang's `int` is `OpTypeInt 32 1`, which MLIR
deserialises to `si32`; `llvm.icmp` and `llvm.add` require signless `i32`.
glslang **never** emits signless integers, and MLIR has no normalising pass (all
nine `--spirv-*` passes checked). Using `uint` does not help — it produces
*mixed* signedness (`"spirv.IAdd"(%a, %b) : (i32, si32) -> i32`) and fails at
deserialisation.

Rewriting the types is sound because SPIR-V carries signedness in the
**operations** (`SLessThan` vs `ULessThan`), not the types. But it is a regex on
an IR dump, and it is what makes glslang output convertible at all. It is safe
for the integer kernels this route supports; a shader mixing signed and unsigned
arithmetic needs this re-examined.

---

## Shader constraints

MLIR's `--convert-spirv-to-llvm` was written for MLIR-*generated* SPIR-V, not
glslang output — its storage-class whitelist exists because it is *"required by
SPIR-V runner"* (`SPIRVToLLVM.cpp:761`). A shader must therefore avoid:

| Not allowed | Where it is rejected | Instead |
|---|---|---|
| Push constants | storage-class switch, `SPIRVToLLVM.cpp:761` | put the values in a storage buffer |
| Uniform blocks | same | use a `buffer` (storage) block |
| `readonly` / `writeonly` | `convertStructType()`, `SPIRVToLLVM.cpp:298` — bails on **any** member decoration | omit the qualifiers |
| Runtime arrays (`int data[];`) | type converter — `spirv.rtarray` does not convert | fixed size (`int data[1024];`) |
| `float`, `vec*`, `mat*` | the CGRA itself — see [Limits](#limits) | integers only |

Named blocks and signed ints are handled by the pipeline (steps 2 and 4).
`check_shader()` tests the source for all of the above before running anything
and names the rule and the reason, rather than letting MLIR fail obscurely five
tools later.

A shader that satisfies all of the above:

```glsl
#version 450
layout(local_size_x = 1, local_size_y = 1, local_size_z = 1) in;

layout(set = 0, binding = 0) buffer DataBuffer  { int data[1024];   };
layout(set = 0, binding = 1) buffer OutBuffer   { int result[1024]; };
layout(set = 0, binding = 2) buffer ParamBuffer { int N; int init;  };

void main() {
    int acc = init;
    for (int i = 0; i < N; i++) {
        acc += data[i];
    }
    result[gl_GlobalInvocationID.x] = acc;
}
```

---

## Buffers become globals, not arguments

MLIR's `GlobalVariablePattern` lowers descriptor bindings to `llvm.mlir.global`,
where a C kernel would have pointer arguments. This is a non-issue:
`cgra-extract` has a `LiveInFromGVar` category, and the resulting DFG differs
from the argument form by exactly one row:

```
11 live_in LiveInFromGVar 28 …      # GLSL route (global)
11 live_in LiveInFromArg  28 …      # C route (function argument)
```

Column-role inference, trip-count detection and sweep sizing all behave
identically.

---

## The oracle (`--ref-src`)

`cgra_gen.py` uses `--ref-src` for two host-side purposes only:

1. The CPU **reference function** written into `verify.c`. It finds the
   `#pragma cgra acc` textually, extracts the enclosing function, strips the
   pragma, renames it `<fn>_ref()`. `sweep.c` then calls it on the same buffers
   the CGRA read and compares — that is what `errors=0` / `### PASS ###` means.
2. The **sweep bound**: `clang.cindex` parses the C loop condition to find the
   trip-count parameter and cross-checks it against the CMEM
   ("Loop condition and CMEM agree").

It has **no effect on the CGRA bitstream** — verified by generating the same app
twice with different `--ref-src`: `cgra_bitstream.c` came out byte-identical,
only `verify.c` and `sweep.c` changed. The shader is the sole source of truth for
what the hardware executes.

`--ref-src` is optional; without it the app still builds, but `verify.c` is a
TODO stub and the sweep bound is guessed, so a `PASS` would be meaningless. The
script warns.

**This is the ugliest part of the design.** For a GLSL flow it is backwards: you
write a shader, then hand-write a C transcription of the same algorithm — with a
pragma you are otherwise not using — in order to verify it. If the two disagree
you are testing the wrong thing. Fixing it properly means either giving
`cgra_gen.py` explicit `--ref-func`/`--loop-bound` flags instead of the
pragma-text scan, or executing the SPIR-V on the host to generate expected
values.

---

## Silent failure modes

Every failure this route has hit reports **exit code 0**. Clean extraction, a
valid schedule, a working build — and wrong numbers. `cgra_glsl.py` greps for the
markers below (`_KNOWN_FAILURE_MARKERS`) and hard-errors; anyone extending this
should assume more exist.

| Symptom | Cause |
|---|---|
| `Instruction Selection will be disabled` | >3 operands on some instruction; every node's opcode left at `-1` |
| `More than 3 operands for inst` | the same, naming the instruction |
| `Unconditional jumps not supported` | un-rotated loop; `CGRAExtract.cpp:865` calls `exit(0)`, no `acc*` dir is written |
| `UNDEF`, `Instruction not supported…` | unhandled opcode reaching `cgra_gen.py` |
| empty `.ll`, no error | nested MLIR module; checked explicitly in `shader_to_tagged_ir()` |
| `cgra_active` constant regardless of `N` | inverted loop-exit polarity (missing `indvars`) |
| results wrong by ~one array element | extra epilog accumulation — `satmapit_parse.py:651` Rule 8; fixed at line 663 by checking both operand fields, since `SADD` is commutative and the mesh operand can land in either |

---

## Limits

- **No floating point, and it would fail silently.** LLVM `FAdd`/`FMul` select
  `FXP_ADD`/`FXPMUL` (fixed-point) and the ISA has no IEEE-754 — see
  `cgra_isa.md`. A float shader would map, build, run and return nonsense, so
  `check_shader()` rejects it. This is a **hardware** limit, not a toolchain one:
  supporting floats means either fixed-point lowering with an agreed scale, or
  different hardware. Most real compute shaders are float-heavy, so this is the
  largest gap in the route.
- **One invocation.** `local_size = 1`, single workgroup. What is mapped is the
  inner loop of a single thread; nothing distributes a dispatch across the grid.
  `gl_GlobalInvocationID` has only been used *outside* the loop (as the output
  index). Mapping a real dispatch is an open design question.
- **One kernel shape verified**: a single counted innermost loop, one buffer
  read, integer accumulate, scalar live-in/live-out — 8 DFG nodes, `II=2`.
  Different shapes will meet `instructionSelection()`'s `default:` case, which
  warns and continues.
- **Restricted shader dialect** (above). All of it is MLIR converter limitation,
  fixable by patching `SPIRVToLLVM.cpp` (runtime arrays, identified structs,
  signedness) — contained and upstreamable, and probably the best next
  investment if this route is to be developed further.

---

## Where to pick up

Roughly in order of value. The first is a decision, not an implementation task,
and it gates the rest.

1. **Decide what happens with floats.** Either declare the route
   integer/fixed-point only (and keep rejecting float shaders, since silence is
   the dangerous outcome), or design the fixed-point lowering — a scale the
   shader author agrees to, applied consistently in the frontend and the host
   buffers. Most real compute shaders are float-heavy, so this determines
   whether the route is a niche accelerator path or a general one.
2. **Multi-invocation dispatch.** Today one invocation's inner loop is mapped.
   How a `local_size > 1` dispatch distributes across the grid is untouched and
   is the real research question — not a toolchain gap.
3. **Patch MLIR's `SPIRVToLLVM.cpp`** for runtime arrays, identified structs
   and signedness, to drop the shader constraints above and remove the
   `si32`→`i32` sed, the least defensible step in the pipeline. The file is
   upstream LLVM (`mlir/lib/Conversion/SPIRVToLLVM/`), so this is not a local
   change: the `mlir-opt` this tool runs is the packaged binary from
   `mlir-20-tools`. Doing it means building MLIR from source and pointing
   `--mlir-dir` at that build, or upstreaming the fix and waiting for it to
   ship. The source is already in the SAT-MapIt tree if you want to read it —
   note it targets MLIR-*generated* SPIR-V, not glslang output, which is why it
   rejects so much.
4. **Replace `cgra_gen.py`'s pragma-text `--ref-src` scan** with explicit
   `--ref-func`/`--loop-bound` inputs, or generate expected values by executing
   the SPIR-V on the host. Removes the last place a GLSL flow needs hand-written
   C.

**Assume more silent failures exist.** Every defect found while building this
route reported exit code 0 — inverted loop polarity, an extra epilog
accumulation, an empty `.ll` from nested MLIR modules, instruction selection
disabling itself for the whole graph. Clean extraction, valid schedule, working
build, wrong numbers. When extending this, verify against a known-good bitstream
or a hardware run, not against the absence of an error message.

---

## Verification status

Four independent routes produce identical behaviour on Verilator — 624602 clock
cycles, byte-identical `SWEEP` output across N ∈ {8…1024}, `### PASS ###`:

| Route | Input |
|---|---|
| `util/cgra_satmap.py` | C with `#pragma cgra acc` (the original baseline) |
| spike-0 | hand-written `.ll`, no clang provenance |
| spike-1a | hand-lowered SPIR-V |
| `util/cgra_glsl.py` | GLSL shader, fully automated |

The `cgra_glsl.py` bitstream is byte-identical to the hand-lowered one, which is
behaviourally identical to the C baseline.

Experiment record, with every intermediate and each failure preserved:
`../spirv2cgra/` — `SPIKE0_NOTES.md` (tag mechanism), `SPIKE1A_NOTES.md`
(shader-shaped loops, the two silent bugs), `SPIKE1B_NOTES.md` (MLIR
automation). That workspace is a scratch area, not a dependency; `cgra_glsl.py`
is self-contained.
