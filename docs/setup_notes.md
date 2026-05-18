# Setup Notes

## Verilator 4.210 on g++ 13+ (historical — project now uses 5.040)

Building 4.210 from source fails because newer g++ no longer implicitly includes `<memory>`. Pass it explicitly:

```bash
make -j$(nproc) CXXFLAGS="-std=c++14 -include memory"
make install
```

## cmake 4.x with bare-metal cross-compiler

cmake 4.x tries to link a test binary during compiler detection, which always fails for bare-metal targets. Fixed in `hw/vendor/esl_epfl_x_heep/sw/cmake/riscv.cmake` by adding:

```cmake
set( CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY )
set( CMAKE_C_COMPILER_WORKS     1 CACHE INTERNAL "" )
set( CMAKE_CXX_COMPILER_WORKS   1 CACHE INTERNAL "" )
```

This replaces the deprecated `CMAKE_FORCE_C_COMPILER` approach already commented out in that file.

## QuestaSim 2022.4_5 compatibility

Two fixes were needed for QuestaSim that Verilator accepts without complaint:

### 1. Forward declaration in `alu.sv`

QuestaSim requires `logic` declarations to appear before any `always_*` block in the same scope, even when referenced only in a preceding `assign`. `abs_result` was declared after the `always_comb` block. Fixed by moving the declaration and its `assign` to before the block.

### 2. False-positive multi-driver error (vopt-7061)

QuestaSim's optimizer flags `vopt-7061` on generate-loop arrays where each iteration writes to a disjoint index slice — the RTL is correct but QuestaSim cannot prove disjointness at the array level. The warning is suppressible. Fixed by passing `-suppress vopt-7061` via `VSIM_USER_OPTIONS` in `run-questasim`.
