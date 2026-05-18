# Upgrade: X-HEEP v1.0.4 + Verilator 5.040 + CORE-V Toolchain

Documents all changes required to upgrade HEEPsilon from the pre-v1.0.4 X-HEEP vendor snapshot
to v1.0.4, upgrade the simulator from Verilator 4.210 to 5.040, and switch the RISC-V toolchain
to CORE-V GCC 14.1.0. These three changes are tightly coupled and must be done together.

**Reference fork:** [UMALabRISCV/HEEPsilon](https://github.com/UMALabRISCV/HEEPsilon) was used as
the primary cross-reference throughout this upgrade. Their repo vendors X-HEEP v1.0.4 and has
already ported the testbench and wrappers to Verilator 5. Where our changes deviate from theirs,
the reason is noted.

---

## 1. Toolchain

### RISC-V compiler: 2022.01.17 → CORE-V 2024.05.30

X-HEEP v1.0.4 changed the default `ARCH` string from `rv32imc` to `rv32imc_zicsr`. The old
toolchain (GCC 11.1.0 / binutils 2.37) does not recognise `zicsr` as a named ISA extension and
fails with:

```
Error: cannot find default versions of the ISA extension `zicsr'
```

Binutils 2.38+ is required. The CORE-V GCC 14.1.0 toolchain satisfies this.

**Install:**
```bash
wget -O /tmp/corev-gcc.tar.gz \
  "https://buildbot.embecosm.com/job/corev-gcc-ubuntu2204/47/artifact/corev-openhw-gcc-ubuntu2204-20240530.tar.gz"
mkdir -p ~/tools/riscv/corev-2024.05.30
tar -xzf /tmp/corev-gcc.tar.gz -C ~/tools/riscv/corev-2024.05.30 --strip-components=1
```

**Compiler prefix:** `riscv32-corev-` (set by X-HEEP v1.0.4's `Makefile` default; no override needed).

**cmake stale cache:** If the SW build directory (`hw/vendor/esl_epfl_x_heep/sw/build/`) was ever
built with the old toolchain, `CMakeCache.txt` will contain `COMPILER_PREFIX=riscv32-unknown-`
and cmake will ignore the new default. Delete the directory before the first build with the new
toolchain:
```bash
rm -rf hw/vendor/esl_epfl_x_heep/sw/build/
```

### cmake: 4.x → 3.31

cmake 4.x changed cross-compilation behaviour for `CMAKE_SYSTEM_NAME = Generic` — it no longer
auto-detects `CMAKE_MAKE_PROGRAM` and the compiler assignment in the toolchain file does not take
effect before cmake's compiler test phase. This breaks SW builds with any bare-metal RISC-V
toolchain regardless of version.

Fix: downgrade cmake inside the conda environment to 3.31:
```bash
conda install -n core-v-mini-mcu -c conda-forge "cmake<4" -y
```

---

## 2. Verilator 4.210 → 5.040

### Removed internals

Verilator 5 removed `Vtestharness__Syms.h` and the `rootp` accessor for probing internal signals
from C++. `tb/tb_top.cpp` must not include that header or use hierarchy paths like
`root->TOP__testharness__heepsilon_top_i__...`.

**Changes in `tb/tb_top.cpp`:**
- Removed `#include "Vtestharness__Syms.h"`
- Replaced all internal hierarchy probes with the public top-level ports `dut->exit_valid_o` and
  `dut->exit_value_o`

### `inout` port connections

Verilator 5 rejects directly assigning a C++ `input wire` top-level port to an `inout logic`
submodule port (ASSIGNIN error). The fix is to add intermediate `wire` signals in the testbench
and route through them:

```systemverilog
wire clk, rst_n, boot_select, execute_from_flash, exit_valid;
assign clk               = clk_i;
assign rst_n             = rst_ni;
assign boot_select       = boot_select_i;
assign execute_from_flash = execute_from_flash_i;
assign exit_valid_o      = exit_valid;
```

### C++ standard: C++11 → C++14

Verilator 5 generated headers use `std::exchange` and `""s` string literals which require C++14.
Update `heepsilon.core`:
```yaml
- '-CFLAGS "-std=c++14 -Wall -g -fpermissive"'
```

### New include file: `heepsilon_clock_config.hh`

`XHEEP_CmdLineOptions.hh` (part of X-HEEP v1.0.4) includes `heepsilon_clock_config.hh` which
must be provided by the top-level project. Create `tb/heepsilon_clock_config.hh`:

```cpp
#ifndef HEEPSILON_CLOCK_CONFIG_HH
#define HEEPSILON_CLOCK_CONFIG_HH
#define HEEPSILON_CPU_CLK_HZ  100000000
#define HEEPSILON_CPU_CLK_KHZ 100000
#define HEEPSILON_CGRA_CLK_HZ  100000000
#define HEEPSILON_CGRA_CLK_KHZ 100000
#endif
```

And register it in `heepsilon.core` under the `tb-verilator` fileset as an include file.

### Lint waivers

Verilator 5 promotes several warnings that were previously ignored. Add to `hw/rtl/cgra_top.vlt`:

```
lint_off -rule WIDTHEXPAND   -file "*hw/core-v-mini-mcu/cpu_subsystem.sv"           -match "*x_issue_req_o*"
lint_off -rule UNUSEDSIGNAL  -file "*hw/core-v-mini-mcu/ao_peripheral_subsystem.sv" -match "*spc2ao_req_i*"
lint_off -rule COMBDLY       -file "*cgra/sim/cgra_clock_gate.sv"                   -match "*"
```

---

## 3. X-HEEP v1.0.4 RTL changes

### New top-level ports on `x_heep_system`

| Port | Direction | How handled |
|---|---|---|
| `hart_id_i` | input [31:0] | Tied to `32'h0` |
| `xheep_instance_id_i` | input [31:0] | Tied to `32'h0` |
| `spi_slave_sck_io` | inout | Left unconnected `()` |
| `spi_slave_cs_io` | inout | Left unconnected `()` |
| `spi_slave_miso_io` | inout | Left unconnected `()` |
| `spi_slave_mosi_io` | inout | Left unconnected `()` |
| `hw_fifo_req_i` | input array | Driven with `empty=1, full=0, alm_full=0, data=0` |
| `hw_fifo_resp_o` | output array | Unconnected |
| `cpu_subsystem_powergate_switch_ack_ni` | input | Connected to top-level port |
| `peripheral_subsystem_powergate_switch_ack_ni` | input | Connected to top-level port |
| `cpu_subsystem_powergate_switch_no` | output | Connected to top-level port |
| `peripheral_subsystem_powergate_switch_no` | output | Connected to top-level port |

GPIO count reduced from 23 (`[22:0]`) to 19 (`[18:0]`); GPIO 14–17 pads repurposed for PDM2PCM/I2S.

### DMA OBI ports: scalar → arrays

All `dma_read_ch0_*`, `dma_write_ch0_*`, `dma_addr_ch0_*` ports on `x_heep_system` were replaced
with arrays indexed by `DMA_NUM_MASTER_PORTS`. Update all signal declarations and connections in
`heepsilon_top.sv` and `ext_xbar.sv` accordingly.

### New package dependency: `fifo_pkg`

`fifo_req_t` / `fifo_resp_t` types for `hw_fifo` ports require `import fifo_pkg::*;` in
`heepsilon_top.sv`.

### Power-gate emulation moved out of testbench

The old testbench used `force` statements to drive power-gate ACK signals deep into the hierarchy.
v1.0.4 exposes these as explicit top-level ports on `heepsilon_top`, which testbench connects to
a shift-register emulation:

```systemverilog
always_ff @(posedge clk) begin : power_switch_emu
  for (int unsigned i = 0; i <= SWITCH_ACK_LATENCY; i++) begin
    if (i == 0) begin
      cpu_subsystem_powergate_switch_ack_n[0]        <= cpu_subsystem_powergate_switch_n;
      peripheral_subsystem_powergate_switch_ack_n[0] <= peripheral_subsystem_powergate_switch_n;
    end else begin
      cpu_subsystem_powergate_switch_ack_n[i]        <= cpu_subsystem_powergate_switch_ack_n[i-1];
      peripheral_subsystem_powergate_switch_ack_n[i] <= peripheral_subsystem_powergate_switch_ack_n[i-1];
    end
  end
end
```

### `ext_xbar.sv`: vendor override required

X-HEEP v1.0.4's `tb/ext_xbar.sv` imports `testharness_pkg` which does not exist in HEEPsilon's
testbench. The file also contains a NAPOT address translation bug. Use
`hw/rtl/ext_xbar.sv` (our override, sourced from the UMA fork) instead. This is already wired
in `heepsilon.core`'s `ext_bus` fileset.

### Code generation template fix: `tb/tb_util.svh.tpl`

X-HEEP v1.0.4 moved the RAM bank iterator from the top-level `XHeep` object to a `MemorySS`
sub-object. Three call sites in the template must change:

```mako
# Wrong (v1.0.3 and earlier):
% for bank in xheep.iter_ram_banks():

# Correct (v1.0.4):
<%  memory_ss = xheep.memory_ss() %>
% for bank in memory_ss.iter_ram_banks():
```

---

## 4. Deviations from the UMA fork

Not all changes are a straight copy from [UMALabRISCV/HEEPsilon](https://github.com/UMALabRISCV/HEEPsilon).
The table below records where we diverged and why.

### `tb/tb_top.cpp` — deep-hierarchy diagnostics removed

UMA's `tb_top.cpp` includes `Vtestharness__Syms.h` and accesses ~30 internal CPU/xbar signals
via `dut->rootp->testharness__DOT__...` paths for the `+diag_boot` feature. These are debug
diagnostics UMA added during their own bringup.

**Our change:** removed that include and all hierarchy probes, replacing `print_core_diag` with
just `exit_valid_o` and `exit_value_o` (public ports). **Reason:** those internal signal paths
reference CV32E20 (`gen_cv32e20`) — a different CPU variant from the CV32E40P used in HEEPsilon.
The paths would not compile even if `Vtestharness__Syms.h` were available, because the hierarchy
names differ. `+diag_boot` is still present but only prints the two public port values.

### `tb/heepsilon_clock_config.hh` vs `.svh`

UMA provides `tb/heepsilon_clock_config.svh` (a SystemVerilog include used in `testharness.sv`
to drive `CLK_FREQUENCY` from a macro). They also include it in `heepsilon.core`'s `tb-verilator`
fileset as `{is_include_file: true}`.

**Our change:** we provide `tb/heepsilon_clock_config.hh` (C++ header only, included by
`XHEEP_CmdLineOptions.hh`) and hardcode `CLK_FREQUENCY = 'd100_000` in `testharness.sv`.
**Reason:** adding a `.svh` include would require the SVH to be visible to FuseSoC during
elaboration; the `.hh` approach is simpler and sufficient since the C++ side only needs the
constants for timeout calculations, not for driving RTL parameters.

### `tb/testharness.sv` — `ZFINX` default and `USE_EXTERNAL_DEVICE_EXAMPLE`

UMA sets `ZFINX = 1` (Zfinx extension, float-in-integer registers) and removes
`USE_EXTERNAL_DEVICE_EXAMPLE`. Our build keeps `ZFINX = 0` and the `USE_EXTERNAL_DEVICE_EXAMPLE`
parameter. **Reason:** HEEPsilon's CV32E40P configuration does not enable Zfinx; changing it
would alter synthesis results silently. `USE_EXTERNAL_DEVICE_EXAMPLE` is retained for
compatibility with existing simulation scripts.

### `hw/rtl/cgra_top.vlt` — three extra waivers

UMA's `.vlt` file does not contain waivers for `WIDTHEXPAND` (XIF `x_issue_req_o`),
`UNUSEDSIGNAL` (`spc2ao_req_i`), or `COMBDLY` (CGRA clock gate). These signals exist because
HEEPsilon instantiates the OpenEdgeCGRA, which UMA does not modify. Without the waivers,
Verilator 5 treats these as errors.

### `heepsilon.core` — PYNQ-Z1 target retained

UMA removed the `pynq-z1` FuseSoC target and its associated filesets (`ip-fpga-pynq-z1`,
`xdc-fpga-pynq-z1`). We keep them — PYNQ-Z1 is an active hardware target in this repo.

### `heepsilon.core` — `testharness_pkg.sv` not included

UMA's `ext_bus` fileset adds `hw/vendor/esl_epfl_x_heep/tb/testharness_pkg.sv`. That file
imports X-HEEP's `testharness_pkg` which does not exist in HEEPsilon's namespace. Including it
causes an elaboration error. We omit it; `ext_xbar.sv` does not need it.

---

## 5. Files changed summary

| File | Change |
|---|---|
| `env.sh` | Verilator 4.210 → 5.040; RISCV toolchain 2022.01.17 → corev-2024.05.30 |
| `CLAUDE.md` | Same path updates |
| `fpga_run.py` | Same path updates |
| `docs/fpga_workflow.md` | Same path updates |
| `hw/rtl/heepsilon_top.sv` | New ports, DMA arrays, fifo_pkg, powergate logic |
| `tb/testharness.sv` | Bridge wires, powergate emulation, reduced GPIO |
| `tb/tb_top.cpp` | Removed stale Verilator 4 internal headers and hierarchy probes |
| `tb/heepsilon_clock_config.hh` | New file |
| `tb/tb_util.svh.tpl` | Fixed `memory_ss.iter_ram_banks()` at three call sites |
| `hw/rtl/cgra_top.vlt` | Added three Verilator 5 lint waivers |
| `heepsilon.core` | C++14 flag; `heepsilon_clock_config.hh` include; `ext_xbar.sv` path |
