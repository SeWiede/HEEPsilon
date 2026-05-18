# FPGA Workflow (PYNQ-Z1 and ZCU104)

How to build, flash, and run applications on the PYNQ-Z1 board (XC7Z020-1CLG400C).

---

## Dependencies

| Tool | Version | Location | Notes |
|---|---|---|---|
| Vivado WebPACK | 2022.2 | `~/tools/Xilinx/Vivado/2022.2/` | Bitstream synthesis |
| Xilinx cable drivers | — | `/etc/udev/rules.d/52-xilinx-*.rules` | Run installer once (see below) |
| OpenOCD | 0.11.0-rc2 | `~/tools/openocd/bin/` | Must build from source with `--enable-ftdi` |
| RISC-V toolchain | corev-2024.05.30 | `~/tools/riscv/corev-2024.05.30/` | CORE-V GCC 14.1.0 (`riscv32-corev-elf-gcc`) — **no GDB included** |
| gdb-multiarch | system | `sudo apt install gdb-multiarch` | Use instead of `riscv32-corev-elf-gdb` |
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
export PATH="$HOME/tools/verilator/5.040/bin:$PATH"
export PATH="$HOME/tools/openocd/bin:$PATH"
export RISCV="$HOME/tools/riscv/corev-2024.05.30"
export RISCV_XHEEP="$HOME/tools/riscv/corev-2024.05.30"
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

---

# FPGA Workflow (ZCU104)

How to build, flash, and run applications on the ZCU104 board (xczu7ev-ffvc1156-2-e).

## Key difference from PYNQ boards

ZCU104 uses a **flash-load workflow**: the software binary is burned to an external SPI flash via iceprog, and the RISC-V core boots from flash automatically after reset. There is no OpenOCD/GDB step required for normal operation.

All communication channels go through the **single "USB JTAG UART" cable** (J164, the one and only micro-USB connector on the board). The on-board FT4232H chip multiplexes all four interfaces over it:

| FT4232H Channel | Linux device | Function |
|---|---|---|
| A | (JTAG, no ttyUSB) | JTAG — Vivado hardware manager, bitstream programming |
| B | `if01` | PS UART0 (ARM Cortex-A53) |
| C | `if02` | PS UART1 (ARM Cortex-A53) |
| D | **`if03`** | **PL UART** — RISC-V `uart_tx_o` output |

**No adapter or second cable is needed.** The PL UART comes out on FT4232H channel D (`if03`).

The RISC-V `uart_tx_o` is wired to FPGA pin C19 (LVCMOS18, HP bank 28), `uart_rx_i` to A20. The baudrate is **9600** (defined in `sw/device/target/zcu104/x-heep.h`).

## Dependencies

Same as PYNQ-Z1 plus:

| Tool | Notes |
|---|---|
| iceprog | Built from source during `make run-fpga` (in `hw/vendor/esl_epfl_x_heep/sw/vendor/yosyshq_icestorm/iceprog/`) — needs `libftdi-dev` |
| External SPI flash module | Connected to ZCU104 via the SPI flash Pmod pins (L10, J9, M10, K9, M8, K8) |

Install libftdi if not present:
```bash
sudo apt install libftdi-dev libusb-1.0-0-dev
```

## 1. Build the bitstream

```bash
conda run -n core-v-mini-mcu make vivado-fpga FPGA_BOARD=zcu104
```

No `use_bscane_xilinx` flag is used for ZCU104 (the flash-load flow does not need BSCANE2).

Build takes ~1–2 hours. Output:
```
build/eslepfl_systems_heepsilon_0/zcu104-vivado/eslepfl_systems_heepsilon_0.bit
```

## 2. Program the bitstream

```bash
vivado -nolog -nojournal -mode batch -source program_fpga_zcu104.tcl
```

Or use the automated script:
```bash
python3 fpga_run.py --board zcu104 hello_world
```

## 3. Build the software application

Use `LINKER=flash_load` and `TARGET=zcu104`:

```bash
conda run -n core-v-mini-mcu make app PROJECT=hello_world LINKER=flash_load TARGET=zcu104
```

Built binary: `sw/build/main.hex` (used by iceprog).

## 4. Program the SPI flash

**Switch state during flash programming:** all boot switches OFF (RISC-V must not be running to avoid conflicting flash access).

```bash
conda run -n core-v-mini-mcu make run-fpga PROJECT=hello_world FPGA_BOARD=zcu104
```

Or step by step:
```bash
( cd hw/vendor/esl_epfl_x_heep/sw/vendor/yosyshq_icestorm/iceprog && make all )
conda run -n core-v-mini-mcu make flash-prog
```

iceprog uses FT4232H channel B (`-I B`) and VID:PID `0403:6011`.

## 5. Boot from flash and capture UART

After flash programming:

1. Set the `boot_select_i` switch ON (SW1 switch 2 — enables flash boot mode)
2. Press the reset button — RISC-V boots from SPI flash
3. Open UART on FT4232H channel D (interface 3):

```bash
picocom -b 9600 -r -l --imap lfcrlf \
  /dev/serial/by-id/usb-Xilinx_JTAG+3Serial_XXXXXXXX-if03-port0
```

Replace `XXXXXXXX` with the serial number visible in `ls /dev/serial/by-id/`.

For `hello_world`, expected output: `hello world!`

## Switch reference

| Signal | XDC pin | Meaning when ON (high) |
|---|---|---|
| `boot_select_i` | E4 | Boot from SPI flash |
| `execute_from_flash_i` | D4 | Execute directly from flash (XIP mode — use `flash_exec` linker) |

For `flash_load`: `boot_select_i=1`, `execute_from_flash_i=0`.

## JTAG / GDB debugging (optional, advanced)

The current ZCU104 bitstream does **not** include BSCANE2 (the `use_bscane_xilinx` flag is not set). FT4232H channel A connects to the ARM/PL JTAG chain, not to the RISC-V debug module.

Two paths to RISC-V debugging:

### Option A: External JTAG cable on Pmod J87 (works with current bitstream)

Wire a Digilent HS2 or FTDI FT232H to Pmod J87 (Bank 26, LVCMOS33):

| Signal | FPGA pin |
|---|---|
| TCK | H6 |
| TMS | G7 |
| TDI | H8 |
| TDO | J6 |
| TRST | M9 |

OpenOCD config: `hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-zcu104-bscan.cfg`

### Option B: BSCANE2 via onboard FT4232H (requires bitstream rebuild)

Add `use_bscane_xilinx` to the ZCU104 target in `heepsilon.core` and rebuild. This tunnels the RISC-V JTAG through the PL config tap, accessible via FT4232H channel A — no external cable needed. The OpenOCD config would need the ZynqMP tap chain (ARM DAP irlen=4 + PL config tap irlen=12) and appropriate USER instruction IR values.
