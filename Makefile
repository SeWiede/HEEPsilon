# Copyright EPFL contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0


# Makefile to generates heepsilon files and build the design with fusesoc

.PHONY: clean help

TARGET 		?= sim
FPGA_BOARD 	?= pynq-z2
# SW compilation target for FPGA runs; mirrors FPGA_BOARD but can be overridden.
# pynq-z1 reuses the pynq-z2 SW target (same clock/memory).
FPGA_TARGET	?= $(FPGA_BOARD)
PORT		?= /dev/ttyUSB2

# 1 external domain for the CGRA
EXTERNAL_DOMAINS = 1
PROJECT ?= hello_world

#MEMORY_BANKS_IL ?= 4 # Power of 2
  
export HEEP_DIR = hw/vendor/esl_epfl_x_heep/
include $(HEEP_DIR)Makefile.venv

# ─── CGRA grid configuration ──────────────────────────────────────────────────
# CGRA_CFG selects which grid the RTL, the SW driver header and the bitstream
# generator are produced for.  Each name maps to cfg/heepsilon_cfg_<name>.hjson;
# 4x4 is a symlink to the repo-root heepsilon_cfg.hjson, so it stays the single
# source of truth for the default grid.
#
#   make mcu-gen        CGRA_CFG=3x3
#   make vivado-fpga    CGRA_CFG=3x3 FPGA_BOARD=zcu104 FUSESOC_FLAGS=--flag=use_bscane_xilinx
#   make archive-fpga   CGRA_CFG=3x3 FPGA_BOARD=zcu104
#
# Each config builds into its own FuseSoC build root, so switching grids never
# touches another grid's simulator or bitstream.
CGRA_CFG       ?= 4x4
HEEPSILON_CFG  ?= cfg/heepsilon_cfg_$(CGRA_CFG).hjson

# RAM banks (32 kB each) for the CPU.  This sets BOTH the synthesised RAM and
# the linker script, so it must match the bitstream actually loaded on the FPGA.
# 4x4 is pinned to 6 because that is what the archived 4x4 ZCU104 bitstream
# contains (verified: gen_sram[0..5] in its synthesis log).  Raising it means
# re-synthesising 4x4 — with a 12-bank linker script on a 6-bank bitstream,
# anything placed above 192 kB lands in nonexistent memory and fails silently.
ifeq ($(CGRA_CFG),4x4)
MEMORY_BANKS   ?= 6
else
MEMORY_BANKS   ?= 12
endif

# 4x4 keeps FuseSoC's historical default path so existing build trees, run.py
# and any hardcoded paths keep working untouched.
ifeq ($(CGRA_CFG),4x4)
FUSESOC_BUILD_ROOT ?= build/eslepfl_systems_heepsilon_0
else
FUSESOC_BUILD_ROOT ?= build/heepsilon_$(CGRA_CFG)
endif
# NOTE: the build root must stay exactly two levels below the repo root —
# heepsilon.core references board files and DPI libs as ../../../<path> relative
# to <build-root>/<target>-<tool>/.
FUSESOC_BUILD  := --build-root $(FUSESOC_BUILD_ROOT)

# Records which grid the generated RTL/SW currently correspond to.
CFG_STAMP      := .heepsilon_active_cfg

.PHONY: check-cfg show-cfg archive-fpga clean-all-builds

## Print the active grid configuration and where its build artefacts live
show-cfg:
	@echo "CGRA_CFG           = $(CGRA_CFG)"
	@echo "HEEPSILON_CFG      = $(HEEPSILON_CFG)"
	@echo "MEMORY_BANKS       = $(MEMORY_BANKS)"
	@echo "FUSESOC_BUILD_ROOT = $(FUSESOC_BUILD_ROOT)"
	@echo "generated for      = $$(cat $(CFG_STAMP) 2>/dev/null || echo '<unknown — run make mcu-gen>')"

# Fails when the checked-out generated files were produced for a different grid.
# Silent when no stamp exists yet (pre-existing trees) — run mcu-gen to create it.
check-cfg:
	@if [ ! -f "$(HEEPSILON_CFG)" ]; then \
	    echo "[heepsilon] ERROR: no such config: $(HEEPSILON_CFG)"; \
	    echo "  available: $$(ls cfg/heepsilon_cfg_*.hjson 2>/dev/null | sed 's|cfg/heepsilon_cfg_||;s|\.hjson||' | tr '\n' ' ')"; \
	    exit 1; \
	fi
	@if [ -f "$(CFG_STAMP)" ] && [ "$$(cat $(CFG_STAMP))" != "$(CGRA_CFG)" ]; then \
	    echo "[heepsilon] ERROR: generated files are for CGRA_CFG=$$(cat $(CFG_STAMP)), you asked for $(CGRA_CFG)."; \
	    echo "  Run:  make mcu-gen CGRA_CFG=$(CGRA_CFG)"; \
	    exit 1; \
	fi

heepsilon-gen:
	@if [ ! -f "$(HEEPSILON_CFG)" ]; then \
	    echo "[heepsilon] ERROR: no such config: $(HEEPSILON_CFG)"; \
	    echo "  available: $$(ls cfg/heepsilon_cfg_*.hjson 2>/dev/null | sed 's|cfg/heepsilon_cfg_||;s|\.hjson||' | tr '\n' ' ')"; \
	    exit 1; \
	fi
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir hw/vendor/esl_epfl_cgra/hw/rtl --pkg-sv hw/vendor/esl_epfl_cgra/hw/rtl/cgra_pkg.sv.tpl
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir hw/vendor/esl_epfl_cgra/hw/rtl --tpl-sv hw/vendor/esl_epfl_cgra/hw/rtl/peripheral_regs.sv.tpl
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir hw/vendor/esl_epfl_cgra/util --tpl-sv hw/vendor/esl_epfl_cgra/util/cgra_bitstream_gen.py.tpl
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir hw/rtl --pkg-sv hw/rtl/heepsilon_pkg.sv.tpl
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir sw/external/drivers/cgra --header-c sw/external/drivers/cgra/cgra.h.tpl
	$(PYTHON) util/heepsilon_gen.py --cfg $(HEEPSILON_CFG) --outdir hw/vendor/esl_epfl_cgra/data --pkg-sv hw/vendor/esl_epfl_cgra/data/cgra_regs.hjson.tpl
	bash -c "cd hw/vendor/esl_epfl_cgra/data; source cgra_reg_gen.sh; cd ../../../.."

# Generates mcu files. First the mcu-gen from X-HEEP is called.
# This is needed to be done after the X-HEEP mcu-gen because the test-bench to be used is the one from heepsilon, not the one from X-HEEP.
mcu-gen: heepsilon-gen
	$(MAKE) -C $(HEEP_DIR) mcu-gen \
	  EXTERNAL_DOMAINS=$(EXTERNAL_DOMAINS) \
	  MEMORY_BANKS=$(MEMORY_BANKS) \
	  EXTERNAL_MCU_GEN_TEMPLATES="$(CURDIR)/tb/tb_util.svh.tpl" \
	  SOURCE=../../../sw/ \
	  REGTOOL="$(CURDIR)/$(HEEP_DIR)hw/vendor/pulp_platform/register_interface/vendor/lowrisc_opentitan/util/regtool.py" \
	  PERIPH_STRUCTS_GEN="$(CURDIR)/$(HEEP_DIR)util/periph_structs_gen/periph_structs_gen.py" \
	  TEMPLATE_FILE="$(CURDIR)/$(HEEP_DIR)util/periph_structs_gen/periph_structs.tpl"
	@echo "$(CGRA_CFG)" > $(CFG_STAMP)
	@echo "[heepsilon] generated files are now for CGRA_CFG=$(CGRA_CFG) (MEMORY_BANKS=$(MEMORY_BANKS))"

## Builds (synthesis and implementation) the bitstream for the FPGA version using Vivado
## @param FPGA_BOARD=nexys-a7-100t,pynq-z2,pynq-z1,zcu104
## @param CGRA_CFG=4x4(default),3x3,5x5
## @param FUSESOC_FLAGS=--flag=<flagname>
vivado-fpga: check-cfg |venv
	fusesoc --cores-root . run --no-export $(FUSESOC_BUILD) --target=$(FPGA_BOARD) $(FUSESOC_FLAGS) --setup --build eslepfl:systems:heepsilon 2>&1 | tee buildvivado_$(CGRA_CFG).log

## Copy a finished bitstream out of build/ into fpga_builds/<CGRA_CFG>/<FPGA_BOARD>/
## so a later build for another grid cannot overwrite it.
## @param CGRA_CFG=4x4(default),3x3,5x5
## @param FPGA_BOARD=zcu104,pynq-z2,...
archive-fpga:
	@BIT="$(FUSESOC_BUILD_ROOT)/$(FPGA_BOARD)-vivado/eslepfl_systems_heepsilon_0.bit"; \
	DST="fpga_builds/$(CGRA_CFG)/$(FPGA_BOARD)"; \
	if [ ! -f "$$BIT" ]; then echo "[heepsilon] ERROR: no bitstream at $$BIT"; exit 1; fi; \
	mkdir -p "$$DST"; \
	cp "$$BIT" "$$DST/"; \
	cp "$(HEEPSILON_CFG)" "fpga_builds/$(CGRA_CFG)/heepsilon_cfg.hjson"; \
	SYNTH="$(FUSESOC_BUILD_ROOT)/$(FPGA_BOARD)-vivado/eslepfl_systems_heepsilon_0.runs/synth_1/runme.log"; \
	BANKS=$$(grep -oE 'gen_sram\[[0-9]+\]' "$$SYNTH" 2>/dev/null | sort -u | wc -l); \
	if grep -qi BSCANE2 "$$SYNTH" 2>/dev/null; then BSCAN=yes; else BSCAN=no; fi; \
	{ echo "cgra_cfg      : $(CGRA_CFG)"; \
	  echo "board         : $(FPGA_BOARD)"; \
	  echo "memory_banks  : $${BANKS:-?}   (counted in the synthesis log, not assumed)"; \
	  echo "bscane2       : $$BSCAN"; \
	  echo "built         : $$(date -Iseconds -r "$$BIT")"; \
	  echo "archived      : $$(date -Iseconds)"; \
	  echo "git           : $$(git rev-parse --short HEAD 2>/dev/null)"; \
	} > "fpga_builds/$(CGRA_CFG)/manifest_$(FPGA_BOARD).txt"; \
	echo "[heepsilon] archived -> $$DST/eslepfl_systems_heepsilon_0.bit"


# Runs verible formating
verible:
	util/format-verible;

# Simulation
verilator-sim: check-cfg
	fusesoc --cores-root . run --no-export $(FUSESOC_BUILD) --target=sim --tool=verilator $(FUSESOC_FLAGS) --setup --build eslepfl:systems:heepsilon 2>&1 | tee buildsim_$(CGRA_CFG).log

questasim-sim: check-cfg
	fusesoc --cores-root . run --no-export $(FUSESOC_BUILD) --target=sim --tool=modelsim $(FUSESOC_FLAGS) --setup --build eslepfl:systems:heepsilon 2>&1 | tee buildsim_$(CGRA_CFG).log

questasim-sim-opt: questasim-sim
	$(MAKE) -C $(FUSESOC_BUILD_ROOT)/sim-modelsim opt

vcs-sim: check-cfg
	fusesoc --cores-root . run --no-export $(FUSESOC_BUILD) --target=sim --tool=vcs $(FUSESOC_FLAGS) --setup --build eslepfl:systems:heepsilon 2>&1 | tee buildsim_$(CGRA_CFG).log


## Generates the build output for a given application
## Uses verilator to simulate the HW model and run the FW
## UART Dumping in uart0.log to show recollected results
run-verilator: check-cfg
	$(MAKE) app PROJECT=$(PROJECT)
	cd ./$(FUSESOC_BUILD_ROOT)/sim-verilator; \
	./Vtestharness +firmware=../../../sw/build/main.hex; \
	cat uart0.log; \
	cd ../../..;

## Generates the build output for a given application
## Uses questasim to simulate the HW model and run the FW
## UART Dumping in uart0.log to show recollected results
run-questasim: check-cfg
	$(MAKE) app PROJECT=$(PROJECT)
	cd ./$(FUSESOC_BUILD_ROOT)/sim-modelsim; \
	make run PLUSARGS="c firmware=../../../sw/build/main.hex" VSIM_USER_OPTIONS="-suppress vopt-7061"; \
	cat uart0.log; \
	cd ../../..;


# Builds the program and uses flash-load to run on the FPGA
run-fpga:
	$(MAKE) app PROJECT=$(PROJECT) LINKER=flash_load TARGET=$(FPGA_TARGET)
	( cd hw/vendor/esl_epfl_x_heep/sw/vendor/yosyshq_icestorm/iceprog && make clean && make all ) ;\
	$(MAKE) flash-prog ;\

# Builds the program and uses flash-load to run on the FPGA.
# Additionally opens picocom (if available) to see the output.
run-fpga-com:
	$(MAKE) app PROJECT=$(PROJECT) LINKER=flash_load TARGET=$(FPGA_TARGET)
	( cd hw/vendor/esl_epfl_x_heep/sw/vendor/yosyshq_icestorm/iceprog && make clean && make all ) ;\
	$(MAKE) flash-prog ;\
	picocom -b 115200 -r -l --imap lfcrlf $(PORT)

XHEEP_MAKE = $(HEEP_DIR)/external.mk
include $(XHEEP_MAKE)

# ─── Per-application incremental SW build ─────────────────────────────────────
# Each PROJECT gets its own CMake build tree under sw/builds/<PROJECT>/.
# sw/build is a symlink that always points at the most recently built app, so
# all downstream targets (run-verilator, run-fpga, …) still find main.hex at
# sw/build/main.hex without any changes.
#
# cmake is only re-configured when build-affecting parameters actually change
# (PROJECT, TARGET, LINKER, ARCH, COMPILER, …); object files are reused
# otherwise. No X-HEEP vendored files are modified.
#
# Usage:
#   make app PROJECT=cgra_func_test            # incremental build
#   make app PROJECT=cgra_func_test LINKER=flash_load   # reconfigures, then builds
#   make clean-app PROJECT=cgra_func_test      # wipe one app's cache
#   make clean-app PROJECT=all                 # wipe all app caches

HEEP_SW_DIR         := $(CURDIR)/$(HEEP_DIR)sw
SW_BUILDS_DIR       := $(CURDIR)/sw/builds
# Object files are grid-specific (cgra.h, linker script), so each CGRA_CFG owns
# a separate cache instead of invalidating the other grid's on every switch.
APP_BUILD_SUBDIR    := $(CGRA_CFG)/$(PROJECT)
APP_BUILD_DIR       := $(SW_BUILDS_DIR)/$(APP_BUILD_SUBDIR)

# SW build parameter defaults (mirrors X-HEEP Makefile defaults)
LINKER              ?= on_chip
ARCH                ?= rv32imc_zicsr
COMPILER            ?= gcc
COMPILER_PREFIX     ?= $(shell basename $$(ls $(RISCV_XHEEP)/bin/*gcc 2>/dev/null | head -1) | sed 's/elf-gcc$$//')
LINK_FOLDER         ?= $(HEEP_SW_DIR)/linker
COMPILER_FLAGS      ?=
CLANG_LINKER_USE_LD ?= 0
VERBOSE             ?= false

# Stamp encodes all CMake-configuration parameters.
# Written to <build>/.cmake_stamp after a successful configure; a mismatch
# triggers cmake re-configure (incremental — no clean).
APP_DIR             ?= applications
# CGRA_CFG is part of the key: switching grids rewrites cgra.h and the linker
# script, so objects from another grid must not be reused.
_STAMP_KEY := P=$(PROJECT) T=$(TARGET) L=$(LINKER) A=$(ARCH) C=$(COMPILER) CP=$(COMPILER_PREFIX) LF=$(LINK_FOLDER) RX=$(RISCV_XHEEP) AD=$(APP_DIR) CG=$(CGRA_CFG) CF=$(COMPILER_FLAGS)

.PHONY: app clean-app link_build link_rm

## Compile SW application — incremental, per-app build cache in sw/builds/
## @param PROJECT=<app_name>            (default: hello_world)
## @param LINKER=on_chip|flash_load|flash_exec  (default: on_chip)
## @param COMPILER=gcc|clang            (default: gcc)
## @param ARCH=<ISA string>             (default: rv32imc_zicsr)
##
## Requires env.sh variables to be exported before calling make:
##   export PATH="$$HOME/tools/verilator/5.040/bin:$$PATH"
##   export RISCV_XHEEP="$$HOME/tools/riscv/corev-2024.05.30"
##   export RISCV="$$RISCV_XHEEP"
##   export MODEL_TECH="$$HOME/tools/questa/2022.4_5/questasim/linux_x86_64"
##   export PATH="$$MODEL_TECH:$$PATH"
app: check-cfg
	@# Guard: RISCV_XHEEP must be set (exported from env.sh before calling make)
	@if [ -z "$(RISCV_XHEEP)" ]; then \
	    echo "[heepsilon] ERROR: RISCV_XHEEP is not set."; \
	    echo "  Export it first (see env.sh):"; \
	    echo "  export RISCV_XHEEP=\$$HOME/tools/riscv/corev-2024.05.30"; \
	    exit 1; \
	fi
	@mkdir -p $(APP_BUILD_DIR)
	@# Re-configure only when parameters have changed or CMake cache is absent
	@STAMP="$(APP_BUILD_DIR)/.cmake_stamp"; \
	KEY='$(_STAMP_KEY)'; \
	if [ ! -f "$$STAMP" ] || [ "$$(cat $$STAMP)" != "$$KEY" ]; then \
	    echo "[heepsilon] cmake configure: $(PROJECT)  (linker=$(LINKER), arch=$(ARCH), target=$(TARGET))"; \
	    env PATH="$(RISCV_XHEEP)/bin:$(PATH)" \
	        RISCV_XHEEP="$(RISCV_XHEEP)" \
	        RISCV="$(RISCV_XHEEP)" \
	        COMPILER="$(COMPILER)" \
	        COMPILER_PREFIX="$(COMPILER_PREFIX)" \
	        ARCH="$(ARCH)" \
	    cmake -G "Unix Makefiles" \
	        -B "$(APP_BUILD_DIR)" \
	        -S "$(HEEP_SW_DIR)" \
	        -DCMAKE_TOOLCHAIN_FILE="$(HEEP_SW_DIR)/cmake/riscv.cmake" \
	        -DROOT_PROJECT="$(HEEP_SW_DIR)/" \
	        -DSOURCE_PATH="$(CURDIR)/sw/" \
        "-DAPP_DIR:STRING=$(APP_DIR)" \
	        -DTARGET="$(TARGET)" \
	        "-DPROJECT:STRING=$(PROJECT)" \
	        "-DRISCV_XHEEP:STRING=$(RISCV_XHEEP)" \
	        "-DLINK_FOLDER:STRING=$(LINK_FOLDER)" \
	        "-DLINKER:STRING=$(LINKER)" \
	        "-DCOMPILER:STRING=$(COMPILER)" \
	        "-DCOMPILER_PREFIX:STRING=$(COMPILER_PREFIX)" \
	        "-DCOMPILER_FLAGS:STRING=$(COMPILER_FLAGS)" \
	        "-DCLANG_LINKER_USE_LD:BOOL=$(CLANG_LINKER_USE_LD)" \
	        "-DVERBOSE:STRING=$(VERBOSE)" \
	    && echo "$$KEY" > "$$STAMP" \
	    || { echo "[heepsilon] cmake configure FAILED"; exit 1; }; \
	else \
	    echo "[heepsilon] cmake config unchanged, skipping re-configure"; \
	fi
	@echo "[heepsilon] building $(PROJECT)..."
	@$(MAKE) -C $(APP_BUILD_DIR)
	@# Keep sw/build pointing at this app's output (relative symlink)
	@ln -sfn builds/$(APP_BUILD_SUBDIR) $(CURDIR)/sw/build
	@echo "[heepsilon] done  →  sw/build -> sw/builds/$(APP_BUILD_SUBDIR)/"

## Remove build cache for PROJECT under the active CGRA_CFG
## (PROJECT=all wipes every app of that grid; add CGRA_CFG=all to wipe all grids)
clean-app:
	@if [ "$(CGRA_CFG)" = "all" ]; then \
	    echo "[heepsilon] removing all per-app build caches (sw/builds/)..."; \
	    rm -rf $(SW_BUILDS_DIR); \
	elif [ "$(PROJECT)" = "all" ]; then \
	    echo "[heepsilon] removing all $(CGRA_CFG) build caches..."; \
	    rm -rf $(SW_BUILDS_DIR)/$(CGRA_CFG); \
	else \
	    echo "[heepsilon] removing build cache for $(PROJECT) ($(CGRA_CFG))..."; \
	    rm -rf $(APP_BUILD_DIR); \
	fi
	@rm -f $(CURDIR)/sw/build

# sw/build symlink is now managed exclusively by the app target
link_build:
	@:

link_rm:
	@rm -f $(CURDIR)/sw/build

## Remove the build tree of the selected CGRA_CFG only (other grids untouched)
clean:
	rm -rf $(FUSESOC_BUILD_ROOT) buildsim_$(CGRA_CFG).log

## Remove every grid's build tree.  Archived bitstreams in fpga_builds/ survive.
clean-all-builds:
	rm -rf build buildsim.log buildsim_*.log
