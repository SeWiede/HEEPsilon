# Setup Notes

## Verilator 4.210 on g++ 13+

Building from source fails because newer g++ no longer implicitly includes `<memory>`. Pass it explicitly:

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
