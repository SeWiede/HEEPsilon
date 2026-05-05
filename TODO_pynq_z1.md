# PYNQ-Z1 Hardware Bring-Up TODO

## One-time setup (user)

- [x] Install **Vivado WebPACK 2022.2** — installed at `$HOME/tools/xilinx/2022.2/`
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

---

## JTAG options — pick one

Three supported ways to connect OpenOCD to the soft RISC-V core (ordered easiest first):

| Option | Hardware needed | OpenOCD config | Notes |
|---|---|---|---|
| **A. Digilent HS2 cable** | HS2 cable wired to PMOD B | `tb/core-v-mini-mcu-nexsys-hs2.cfg` | Cleanest; existing confirmed config |
| **B. EPFL Programmer** | FT4232H programmer to PMOD B | `tb/core-v-mini-mcu-pynq-z2-esl-programmer.cfg` | Existing confirmed config |
| **C. Onboard USB bscan** | Just the USB cable | `tb/core-v-mini-mcu-pynq-z1-bscan.cfg` | No extra hardware; config unverified (see uncertainties) |

PMOD B pin mapping (from our XDC): TCK=W16, TMS=T11, TRST=W19, TDI=Y14, TDO=V12

Find which USB serial device appeared after plugging in:
```bash
dmesg --time-format iso | grep FTDI
```

---

## Build + run steps (first time)

- [ ] `make mcu-gen` — generate RTL (run once, or after config changes)
- [ ] `make vivado-fpga FPGA_BOARD=pynq-z1` — synthesis + bitstream (~1 h)
  - Bitstream path: `build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/` — confirm exact filename after first run
- [ ] `make app PROJECT=hello_world LINKER=on_chip TARGET=pynq-z1`
- [ ] Program bitstream via **Vivado Hardware Manager**:
  `Open → Hardware Manager → Open Target → Autoconnect → Program Device`
  select the `.bit` file from the build directory above
- [ ] Start UART console (9600 baud, same as pynq-z2):
  ```bash
  picocom -b 9600 -r -l --imap lfcrlf /dev/ttyUSB<N>
  ```
  Replace `<N>` with the port number from `dmesg` output above
- [ ] Start OpenOCD (use whichever option from the table above):
  ```bash
  openocd -f hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-nexsys-hs2.cfg   # option A
  # or
  openocd -f hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z1-bscan.cfg # option C
  ```
- [ ] Load + run via GDB:
  ```
  $RISCV/bin/riscv32-unknown-elf-gdb sw/build/main.elf
  (gdb) set remotetimeout 2000
  (gdb) target remote localhost:3333
  (gdb) load
  (gdb) continue
  ```
  Expected UART output: `hello world!`

---

## Uncertainties / things to verify

- [ ] **OpenOCD bscan config (option C) `ftdi_layout_init`**
  - Currently `0x0088 0x008b` — same as PYNQ-Z2 (TUL board)
  - PYNQ-Z1 is Digilent-made; if bscan fails at `scan_chain`, try `0x3088 0x1f8b` (Digilent Zybo value)
  - Workaround: use HS2 cable (option A) which has a confirmed working config

- [ ] **UART port number** — find with `dmesg --time-format iso | grep FTDI` after plugging in
  - X-HEEP docs use `/dev/ttyUSB2` for pynq-z2; PYNQ-Z1 may differ

- [ ] **Bitstream fits?** — XC7Z020 has 53K LUTs; 4×4 CGRA + MCU is large
  - Check utilization report after synthesis; if >85% LUT or timing fails:
  - Reduce CGRA in `heepsilon_cfg.hjson` (e.g. 4×3 or 3×3) and re-run `mcu-gen`

- [ ] **`use_bscane_xilinx` flag** — X-HEEP docs mention this flag for bscan builds:
  ```bash
  make vivado-fpga FPGA_BOARD=pynq-z1 FUSESOC_FLAGS=--flag=use_bscane_xilinx
  ```
  Unclear if this is required for the bscan OpenOCD config to work; try without first

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
