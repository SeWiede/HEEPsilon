# FPGA Workflow (PYNQ-Z1)

How to build, flash, and run applications on the PYNQ-Z1 board (XC7Z020-1CLG400C).

---

## Dependencies

| Tool | Version | Location | Notes |
|---|---|---|---|
| Vivado WebPACK | 2022.2 | `~/tools/Xilinx/Vivado/2022.2/` | Bitstream synthesis |
| Xilinx cable drivers | — | `/etc/udev/rules.d/52-xilinx-*.rules` | Run installer once (see below) |
| OpenOCD | 0.11.0-rc2 | `~/tools/openocd/bin/` | Must build from source with `--enable-ftdi` |
| RISC-V toolchain | 2022.01.17 | `~/tools/riscv/2022.01.17/` | Compiler only — **no GDB included** |
| gdb-multiarch | system | `sudo apt install gdb-multiarch` | Use instead of `riscv32-unknown-elf-gdb` |
| picocom | system | `sudo apt install picocom` | UART console |
| FuseSoC / conda env | — | `core-v-mini-mcu` conda env | Required for `make` targets |
| USB-TTL serial adapter | — | e.g. CP2102, CH340 | 3.3V logic — needed for UART (see step 4) |

### Installing Xilinx cable drivers (one-time, new machine)

```bash
cd ~/tools/Xilinx/Vivado/2022.2/data/xicom/cable_drivers/lin64/install_script/install_drivers
sudo ./install_drivers
```

Replug the board after installing. Add yourself to `dialout` to avoid needing `sudo` for picocom:

```bash
sudo usermod -a -G dialout $USER   # log out and back in after this
```

### libtinfo5 / libncurses5 on Ubuntu 22.04+

These packages no longer exist. Create symlinks before running the Vivado installer:

```bash
sudo ln -sf /usr/lib/x86_64-linux-gnu/libtinfo.so.6 /usr/lib/x86_64-linux-gnu/libtinfo.so.5
sudo ln -sf /usr/lib/x86_64-linux-gnu/libncursesw.so.6 /usr/lib/x86_64-linux-gnu/libncursesw.so.5
```

---

## Environment setup

```bash
export XILINX_VIVADO="$HOME/tools/Xilinx/Vivado/2022.2"
export PATH="$XILINX_VIVADO/bin:$PATH"
export PATH="$HOME/tools/verilator/4.210/bin:$PATH"
export PATH="$HOME/tools/openocd/bin:$PATH"
export RISCV="$HOME/tools/riscv/2022.01.17"
export RISCV_XHEEP="$HOME/tools/riscv/2022.01.17"
```

---

## 1. Build the bitstream

```bash
conda run -n core-v-mini-mcu make vivado-fpga FPGA_BOARD=pynq-z1 FUSESOC_FLAGS=--flag=use_bscane_xilinx
```

**`use_bscane_xilinx` is required.** Without it, the RISC-V debug module is unreachable through
the onboard USB JTAG chain. See [How BSCANE2 works](#how-bscane2-works) below.

Build takes ~1 hour. Output:
```
build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit
```

Timing on XC7Z020 with 4×4 CGRA: WNS ≈ 15 ns, WHS > 0 — healthy margin.

---

## 2. Flash the bitstream

```bash
vivado -nolog -nojournal -mode batch -source program_fpga.tcl
```

`program_fpga.tcl` (at project root) handles device selection automatically. Successful flash prints:
```
End of startup status: HIGH
```

**The bitstream is volatile — lost on every power cycle. Reflash after every power-on.**

---

## 3. Build the software application

```bash
conda run -n core-v-mini-mcu make app PROJECT=<app_name> LINKER=on_chip TARGET=pynq-z1
```

Built ELF: `sw/build/main.elf`. Available applications are listed in `sw/applications/`.

---

## 4. UART console

The PYNQ-Z1 FT2232H UART connects to PS MIO14/15 (ARM UART), **not** to PL pins. The soft
RISC-V `uart_tx_o` comes out on W14 = **PMOD B pin 1** (physical header). A USB-TTL adapter
is required — there is no UART path over the PROG USB cable alone.

**Wiring (3.3V logic adapter):**

| USB-TTL adapter | PMOD B pin | FPGA pin |
|---|---|---|
| RXD | Pin 1 (top-left) | W14 — `uart_tx_o` |
| GND | Pin 5 | GND |
| TXD, 3V3, 5V | — | **leave unconnected** |

Use `/dev/serial/by-id/` for a stable port name independent of plug order:

```bash
ls /dev/serial/by-id/
```

CP2102 example:
```bash
picocom -b 9600 -r -l --imap lfcrlf \
  /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
```

Open picocom **before** starting GDB so you don't miss early output.

---

## 5. Start OpenOCD

```bash
openocd -f hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z1-bscan.cfg
```

Successful startup prints:
```
Examined RISC-V core; found 1 harts
Ready for Remote Connections
```

**OpenOCD version note:** The config uses 0.11.0-rc2 syntax (`ftdi_vid_pid`, `ftdi_channel`,
`ftdi_layout_init` with underscores). Later versions use subcommand style and will fail with
`invalid command name "ftdi"`.

---

## 6. Load and run via GDB

```bash
gdb-multiarch sw/build/main.elf
```

```
(gdb) set remotetimeout 2000
(gdb) target remote localhost:3333
(gdb) load
(gdb) continue
```

For `hello_world`, expected picocom output: `hello world!`

---

## How BSCANE2 works

`use_bscane_xilinx` is about **getting code onto the board** — it controls whether GDB can reach
the RISC-V core over the onboard PROG USB cable. It has nothing to do with UART.

The RISC-V JTAG port is always wired to PMOD B pins (see table below). The flag adds a second
path to the same port — tunnelling it through the onboard USB via a `BSCANE2` primitive.

| Without flag | With flag |
|---|---|
| JTAG only accessible via external cable on PMOD B | JTAG accessible via onboard PROG USB (no external cable needed) |
| OpenOCD: `dtmcontrol` reads 0, cannot connect | OpenOCD: `Examined RISC-V core; found 1 harts` |

### How the tunnel works

The Zynq's onboard USB (FT2232H) exposes a JTAG chain with two taps: the FPGA config tap
(`0x23727093`) and the ARM DAP (`0x4ba00477`). The FPGA config tap has reserved "user"
JTAG instructions (USER1–USER4) for routing custom data into FPGA logic.

With `use_bscane_xilinx`, FuseSoC instantiates a `BSCANE2` primitive connected to the RISC-V
debug transport module (DTM). It intercepts USER2 and USER3 and routes TDI/TDO through the DTM:

```
riscv set_ir dtmcs 0x22   # USER2 (6-bit IR) → RISC-V DTM control register
riscv set_ir dmi   0x23   # USER3            → RISC-V debug module interface
```

### Alternative: external JTAG cable on PMOD B

If you have a Digilent HS2 cable, you can skip the flag and wire directly to PMOD B. Pin
assignments from `hw/fpga_cgra/constraints/pynq-z1/pin_assign.xdc`:

| Signal | FPGA pin |
|---|---|
| TCK | W16 |
| TMS | T11 |
| TRST | W19 |
| TDI | Y14 |
| TDO | V12 |

Use `hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-nexsys-hs2.cfg` as the OpenOCD config.
Note: which PMOD B physical pins these map to has not been verified against the Digilent schematic.
