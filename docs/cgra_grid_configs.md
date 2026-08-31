# Multiple CGRA grid sizes

HEEPsilon can be built for more than one CGRA grid. Each grid gets its own
FuseSoC build tree, so simulators and bitstreams for different grids coexist and
building one never overwrites another.

| `CGRA_CFG` | Config file | Build root |
|---|---|---|
| `4x4` (default) | `cfg/heepsilon_cfg_4x4.hjson` → `heepsilon_cfg.hjson` | `build/eslepfl_systems_heepsilon_0/` |
| `3x3` | `cfg/heepsilon_cfg_3x3.hjson` | `build/heepsilon_3x3/` |
| `5x5` | `cfg/heepsilon_cfg_5x5.hjson` | `build/heepsilon_5x5/` |

`4x4` keeps FuseSoC's historical path, so existing trees and scripts are
unaffected.

## Building

```bash
# 1. Regenerate RTL + SW for the grid  (rewrites cgra_pkg.sv, cgra.h, linker script, …)
make mcu-gen CGRA_CFG=3x3

# 2. Bitstream  (~1-2 h)
make vivado-fpga CGRA_CFG=3x3 FPGA_BOARD=zcu104 FUSESOC_FLAGS=--flag=use_bscane_xilinx

# 3. Copy it somewhere a later build cannot touch
make archive-fpga CGRA_CFG=3x3 FPGA_BOARD=zcu104
#    -> fpga_builds/3x3/zcu104/eslepfl_systems_heepsilon_0.bit
#       fpga_builds/3x3/manifest_zcu104.txt

# Simulators work the same way
make verilator-sim CGRA_CFG=3x3
make run-verilator CGRA_CFG=3x3 PROJECT=<app>
```

`make show-cfg` prints the selected grid, its build root, and which grid the
generated files currently belong to.

## Running a test on a given grid

`run.py --cgra-cfg` is the short path: it runs `mcu-gen` itself when the
generated tree belongs to a different grid, so switching is one command.

```bash
./run.py --cgra-cfg 3x3 --simulator verilator --tests cgra_alu_test
./run.py --cgra-cfg 5x5 --simulator verilator --tests cgra_alu_test
```

The simulator is **not** built automatically — it is a ~10 minute job and each
grid needs its own. Build it once per grid:

```bash
./run.py --cgra-cfg 3x3 --build-sim
```

Without it you get the exact command to run rather than a late
"skipping config" message.

`cgra_alu_test` runs on any grid: it uses column 0 / row 0 only, so one
instruction stream is valid everywhere. It exercises every ALU op and all
branch forms and reports `finished with 0 errors`. Verified on 4x4, 3x3 and 5x5.

Note that its KMEM word is built from `CGRA_CMEM_BK_DEPTH_LOG2` and
`CGRA_RCS_NUM_CREG_LOG2` rather than fixed bit offsets — the `col_mask` field
starts at bit 12 on a 128-deep bank (4x4, 3x3) and at bit 14 on a 512-deep one
(5x5). Any hand-written app that hardcodes those offsets is silently wrong on a
grid with a different `cmem_bk_depth`.

## Only one grid is "generated" at a time

`heepsilon_cfg.hjson` drives generated files that live in the source tree
(`cgra_pkg.sv`, `heepsilon_pkg.sv`, `cgra.h`, `cgra_bitstream_gen.py`, the X-HEEP
linker script, …), all of which are gitignored. Only one grid's version of those
can exist at once, so `make mcu-gen` records the grid in `.heepsilon_active_cfg`
and every build/run target refuses to proceed against a different one:

```
[heepsilon] ERROR: generated files are for CGRA_CFG=3x3, you asked for 4x4.
  Run:  make mcu-gen CGRA_CFG=4x4
```

Switching back is lossless — the generated files are a pure function of the
config plus `MEMORY_BANKS`, verified byte-identical across a switch cycle.
Already-built simulators and bitstreams are untouched by a switch; only the
sources they were built from change.

`run.py` reads the same stamp and picks the matching build root and bitstream
automatically.

## Memory

`MEMORY_BANKS` sets both the synthesised CPU RAM and the linker script, so it
must match the bitstream actually on the FPGA. A 12-bank linker script running
on a 6-bank bitstream puts anything above 192 kB in nonexistent memory: no
error, no UART, just a dead run.

| `CGRA_CFG` | `MEMORY_BANKS` | CPU RAM |
|---|---|---|
| `4x4` | 6 | 192 kB |
| `3x3`, `5x5` | 12 | 384 kB |

4x4 is pinned to 6 because that is what the archived 4x4 ZCU104 bitstream
contains — its synthesis log instantiates `gen_sram[0..5]`. Re-synthesise 4x4 if
you want more. The CGRA's own CMEM array is small next to either figure (4x4:
2 kB, 3x3: 1.5 kB, 5x5: 10 kB).

Passing `MEMORY_BANKS=<N>` still overrides it, as the older docs describe. What
changed is the default: a bare `make mcu-gen` used to fall through to X-HEEP's
2 banks (64 kB), which is why every command in those docs passes the value by
hand. It now defaults to something that matches a real bitstream. `run.py`
likewise no longer hardcodes a count; it defers to the Makefile unless you pass
`--memory-banks`.

## SW must match the grid

A CGRA bitstream — the kernel mapping, not the FPGA image — is grid-specific in
two ways. The column count sets the kernel word's mask width, and the
inter-column mesh is a torus whose wrap edge is `N_COL-1 → 0`. A mapping solved
for 3 columns expects col2's `RCR` to reach col0; on a 4-column build it
physically reaches col3. Nothing rejects that — it is silently wrong.

`sw/external/drivers/cgra/cgra.h` carries `CGRA_N_COLS` / `CGRA_N_ROWS`, so C
code can guard against it, and five applications do:

```
sw/applications/{cgra_func_test,cgra_fft,cgra_fir,cgra_dbl_search,cgra_load_store_test}
```

```c
#if CGRA_N_COLS != 4 | CGRA_N_ROWS != 4
  #error The CGRA must have a 4x4 size to run this example
#endif
```

Each carries a hand-written 4x4 mapping, so the guard is correct — deleting it
would compile a mapping the hardware cannot run. Porting one means re-mapping its
kernel for the target grid. The same applies to
`sw/applications/kernel_test/kernels/*/3x3/`: those reference mappings are valid
only on a 3x3 build.

Two things are grid-agnostic by construction:

- **`cgra_check_conf`** — builds its bitstream at runtime from `CGRA_N_ROWS` /
  `CGRA_MAX_COLS` and checks the result against the expected mesh rotation. Run
  it first on a new grid: it catches a wrong register map, bank depth or column
  count immediately.
- **Anything generated by `util/cgra_satmap.py`** — `cgra_satmap.py` and
  `cgra_gen.py` read the grid from the generated `cgra.h`, so regenerating an app
  under a different `CGRA_CFG` maps it for that grid. `--n-row` / `--n-col`
  override this if you need to map for a grid you have not generated.

## `cmem_bk_depth` is not free-form

The CGRA context memory maps onto a fixed set of Xilinx BRAM IPs in
`hw/fpga_cgra/cgra_sram_wrapper.sv`. Only these depths exist:

```
128, 512, 1024, 2048, 4096, 8192, 16384
```

Anything else fails FPGA synthesis at elaboration:

```
ERROR: [Synth 8-6058] Synth Error: Bank size not generated for NumWords = 256.
```

Verilator and QuestaSim do not care, so this only bites on the Vivado path.

The default, `max_columns * rcs_num_instr`, happens to land on 128 at 4 columns
and is illegal almost everywhere else (96 at 3 columns, 160 at 5). Both new
configs therefore pin it:

| Grid | `cmem_bk_depth` | Note |
|---|---|---|
| 3x3 | 128 | same as 4x4, so `CMEM_BK_DEPTH_LOG2 = 7` and the kernel word's `start_add` field stays bit-compatible |
| 5x5 | 512 | smallest legal depth ≥ the 160 a full-width kernel needs |

## Built results (ZCU104, xczu7ev)

| Grid | LUTs | Registers | BRAM tiles | DSPs | WNS | WHS | Banks |
|---|---|---|---|---|---|---|---|
| 4x4 | 51 803 (22.5 %) | 53 303 | 50 | 65 | +3.487 ns | +0.010 ns | 6 |
| 3x3 | 42 178 (18.3 %) | 44 348 | 97.5 | 37 | +2.961 ns | +0.010 ns | 12 |
| 5x5 | 64 889 (28.2 %) | 65 087 | 98.5 | 101 | +2.794 ns | +0.010 ns | 12 |

All three meet timing with zero failing endpoints. 4x4 uses less BRAM only
because it has 6 memory banks against the others' 12.

`make archive-fpga` counts the banks and checks for BSCANE2 in the build's own
synthesis log rather than trusting the flags you pass it, so
`fpga_builds/<cfg>/manifest_<board>.txt` describes the bitstream as built.

## What has actually been exercised

Verilator, per grid. A bitstream that meets timing is not evidence that the
design computes anything, so these are the runs that make the claim:

| Grid | `cgra_check_conf` | SAT-MapIt `vec_sum` |
|---|---|---|
| 4x4 | 0 errors | 4/4 sweep points pass, 1.20× at N=64 |
| 3x3 | 0 errors (27 502 cycles) | runs, wrong sums — unresolved manual items |
| 5x5 | 0 errors (130 502 cycles) | runs, wrong sums — unresolved manual items |

`cgra_check_conf` drives every PE, checks each result against the expected mesh
rotation, and returns the mismatch count as its exit code — so
`Program Finished with value 0` in the testbench output is the pass signal. Its
`PRINTF`s are compiled out unless you build with `COMPILER_FLAGS=-DDEBUG`; the
exit code is authoritative either way. **This is the load-bearing result**: it
exercises every PE, the mesh, the context memory and the register map at each
grid size, and it passes on all three.

`vec_sum` passing only on 4x4 is a toolchain limit, not a hardware one. On 3x3
and 5x5 the mapper leaves the `init` parameter with no column to map to, and
`cgra_gen.py` says so under *Manual action needed* in each app's
`GENERATION_SUMMARY.md` rather than guessing. Those items are per-kernel
hand-work.

## Grid limits

- **Columns**: `max_columns ≤ 2^(32 - log2(rcs_num_instr) - log2(max_columns * rcs_num_instr))`
  — 20 with the default `rcs_num_instr = 32`. Each column also adds one OBI
  master port to the external crossbar.
- **Rows**: no architectural limit; each row adds one CGRA IMEM bank
  (`N_MEM_BANKS = N_ROW + 1`).
