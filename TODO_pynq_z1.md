# PYNQ-Z1 Hardware Bring-Up TODO

## One-time setup (user)

- [x] Install **Vivado WebPACK 2022.2** — installed at `$HOME/tools/Xilinx/Vivado/2022.2/`
- [x] Install Xilinx cable drivers — done (`52-xilinx-*.rules` installed)

- [x] Install **OpenOCD 0.11.0-rc2** — the exact version tested by X-HEEP docs; build from source:
  ```bash
  # Prerequisites
  sudo apt install pkg-config libftdi1-2 libftdi1-dev libusb-1.0-0-dev gcc-10 g++-10

  # Build
  wget https://sourceforge.net/projects/openocd/files/openocd/0.11.0-rc2/openocd-0.11.0-rc2.tar.gz
  tar xf openocd-0.11.0-rc2.tar.gz && cd openocd-0.11.0-rc2
  ./configure --enable-ftdi --enable-remote-bitbang --prefix=$HOME/tools/openocd
  make && make install
  export PATH=$HOME/tools/openocd/bin:$PATH  # add to .bashrc
  ```
  - NOTE: `sudo apt install openocd` gives a different (likely older) version — avoid it

- [x] Set up FTDI USB access — udev rules installed, `wiede` added to `plugdev` on host:
  ```bash
  # For Digilent HS2 cable (VID 0403, PID 6014):
  echo 'ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6014", MODE="664", GROUP="plugdev"' | \
    sudo tee /etc/udev/rules.d/60-hs2.rules
  # For PYNQ-Z1 onboard FT2232H bscan (VID 0403, PID 6010):
  echo 'ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6010", MODE="664", GROUP="plugdev"' | \
    sudo tee /etc/udev/rules.d/60-ft2232.rules

  sudo udevadm control --reload
  sudo usermod -a -G plugdev $USER
  # log out and back in after this
  ```

- [x] Install picocom

- [x] Install **`gdb-multiarch`** — the RISC-V toolchain 2022.01.17 does NOT include GDB:
  ```bash
  sudo apt install gdb-multiarch
  ```
  Use `gdb-multiarch` everywhere the docs say `riscv32-unknown-elf-gdb`.

---

## JTAG options — pick one

Three supported ways to connect OpenOCD to the soft RISC-V core (ordered easiest first):

| Option | Hardware needed | OpenOCD config | Notes |
|---|---|---|---|
| **A. Digilent HS2 cable** | HS2 cable wired to PMOD B | `tb/core-v-mini-mcu-nexsys-hs2.cfg` | Cleanest; existing confirmed config |
| **B. EPFL Programmer** | FT4232H programmer to PMOD B | `tb/core-v-mini-mcu-pynq-z2-esl-programmer.cfg` | Existing confirmed config |
| **C. Onboard USB bscan** | Just the USB cable | `tb/core-v-mini-mcu-pynq-z1-bscan.cfg` | **Confirmed working** — requires `use_bscane_xilinx` flag at build time (see below) |

PMOD B pin mapping (from our XDC): TCK=W16, TMS=T11, TRST=W19, TDI=Y14, TDO=V12

Find which USB serial device appeared after plugging in:
```bash
dmesg --time-format iso | grep FTDI
```

---

## Build + run steps (first time)

- [x] `make mcu-gen` — generate RTL (run once, or after config changes)
- [x] `make vivado-fpga FPGA_BOARD=pynq-z1 FUSESOC_FLAGS=--flag=use_bscane_xilinx` — synthesis + bitstream (~1 h)
  - **`use_bscane_xilinx` is required** for onboard USB JTAG (option C). Without it the RISC-V debug module is unreachable through the Xilinx JTAG chain (`dtmcontrol` reads 0). See "How BSCANE2 works" below.
  - Bitstream path: `build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit`
- [x] `make app PROJECT=hello_world LINKER=on_chip TARGET=pynq-z1`
- [x] Program bitstream via `program_fpga.tcl` (Vivado batch mode):
  ```bash
  export XILINX_VIVADO="$HOME/tools/Xilinx/Vivado/2022.2" && export PATH="$XILINX_VIVADO/bin:$PATH"
  vivado -nolog -nojournal -mode batch -source program_fpga.tcl
  ```
- [x] Start UART console — **PYNQ-Z1 UART does NOT go through PROG USB**.
  The FT2232H UART connects to PS MIO14/15 (ARM UART), not PL pins. The soft RISC-V
  `uart_tx_o` is on W14 = **PMOD B pin 1**. Requires a USB-TTL adapter (3.3V logic):
  - Adapter RXD → PMOD B pin 1 (top-left, W14)
  - Adapter GND → PMOD B pin 5 (GND)
  - Leave TXD, 3V3, 5V unconnected
  - FT2232H takes ttyUSB0 + ttyUSB1; adapter appears as ttyUSB2
  ```bash
  sudo picocom -b 9600 -r -l --imap lfcrlf /dev/ttyUSB2
  ```
- [x] Start OpenOCD:
  ```bash
  export PATH="$HOME/tools/openocd/bin:$PATH"
  openocd -f hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z1-bscan.cfg
  ```
- [x] Load + run via GDB:
  ```bash
  gdb-multiarch sw/build/main.elf   # riscv32-unknown-elf-gdb not included in 2022.01.17 toolchain
  (gdb) set remotetimeout 2000
  (gdb) target remote localhost:3333
  (gdb) load
  (gdb) continue
  ```
  Expected UART output: `hello world!` — **confirmed working 2026-05-08**

---

## How BSCANE2 / `use_bscane_xilinx` works

The Zynq's onboard USB (FT2232H) exposes a JTAG chain with two taps: the FPGA config tap
(`0x23727093`) and the ARM DAP (`0x4ba00477`). The FPGA config tap has four reserved "user"
JTAG instructions (USER1–USER4) for routing custom data into FPGA logic.

**Without `use_bscane_xilinx`:** the RISC-V JTAG port is only wired to PMOD B pins — the USER
instructions connect to nothing in the fabric, so `dtmcontrol` reads 0 and OpenOCD cannot reach
the debug module.

**With `use_bscane_xilinx`:** FuseSoC instantiates a Xilinx `BSCANE2` primitive connected to the
RISC-V debug transport module (DTM). The BSCANE2 intercepts USER2/USER3 instructions and routes
TDI/TDO through the DTM. The OpenOCD config maps this:
```
riscv set_ir dtmcs 0x22   # USER2 (6-bit IR) → RISC-V DTM control register
riscv set_ir dmi   0x23   # USER3            → RISC-V debug module interface
```
This is why the flag is mandatory for the onboard-USB JTAG path. X-HEEP upstream docs describe it
as "optional" only because you can alternatively use an external JTAG cable on PMOD B instead.

---

## Uncertainties / things to verify

- [x] **OpenOCD bscan config (option C) `ftdi_layout_init`**
  - `0x0088 0x008b` confirmed working on PYNQ-Z1 (same Digilent FT2232H family as Z2)

- [ ] **UART port number** — find with `dmesg --time-format iso | grep FTDI` after plugging in
  - On this PC: `/dev/ttyUSB1` (FT2232H channel B). May differ if other USB-serial devices present.

- [x] **Bitstream fits?** — 4×4 CGRA + MCU fits on XC7Z020; timing clean (WNS=15.3 ns, WHS=0.013 ns)

- [x] **`use_bscane_xilinx` flag** — **confirmed required** for bscan (option C). Without it,
  `dtmcontrol` reads 0. Always build with:
  ```bash
  make vivado-fpga FPGA_BOARD=pynq-z1 FUSESOC_FLAGS=--flag=use_bscane_xilinx
  ```

- [x] **OpenOCD config syntax** — `core-v-mini-mcu-pynq-z1-bscan.cfg` uses old-style OpenOCD
  0.11.0-rc2 syntax (`ftdi_vid_pid`, `ftdi_channel`, `ftdi_layout_init` with underscores).
  Newer OpenOCD uses `ftdi vid_pid` etc. (subcommands). Config is correct for 0.11.0-rc2.

---

## Known issues / pre-existing bugs to fix later

- [ ] `heepsilon.core` line 115 references `hw/fpga/constraints/pynq-z2/constraints.xdc`
  - Path `hw/fpga/` does not exist at project root; likely should be `hw/fpga_cgra/`
  - Hasn't caused failures yet (X-HEEP dependency may supply this XDC)
  - pynq-z1 uses the correct path so not a blocker for us

- [ ] `nexys-a7-100t` listed in Makefile FPGA_BOARD comment but has no target in `heepsilon.core`
  - Either add the target or remove it from the comment

---

## Future / nice-to-have

- [ ] **Zybo Z7-20 support** — same XC7Z020 chip; same effort as pynq-z1
  - Needs: `hw/fpga_cgra/constraints/zybo-z7/pin_assign.xdc` (different connector layout)
  - Needs: `hw/fpga_cgra/scripts/zybo-z7/xilinx_generate_clk_wizard.tcl` (125 MHz, same)
  - Needs: new target in `heepsilon.core`, OpenOCD bscan config

- [ ] **Flash-load flow without EPFL programmer**
  - Current `run-fpga` target requires EPFL FT4232 programmer (`iceprog`)
  - Alternative: Vivado HW Manager can program SPI flash directly
    - Needs: identify PYNQ-Z1 flash chip (run `dmesg` or check board markings)
    - Needs: convert `main.hex` → `.bin` or `.mcs` (`objcopy` or `srec_cat`)

- [ ] **Upstreaming** — consider submitting pynq-z1 support as PR to X-HEEP and HEEPsilon
  - X-HEEP: add `ip-fpga-pynq-z1` conditional to `core-v-mini-mcu-fpga.core`
  - X-HEEP: add `sw/device/target/pynq-z1/` directory
