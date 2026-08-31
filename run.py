#!/usr/bin/env python3
"""
run.py — HEEPsilon unified run script.

  --target sim   (default): build + simulate, validate UART output
  --target fpga            : compile + program board, capture UART via OpenOCD/GDB
  --target both            : fpga run, then sim run, then compare outputs line-by-line
"""
from __future__ import annotations

import argparse
import atexit
import itertools
import json
import os
import queue
import re
import signal
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Make SIGTERM behave like Ctrl-C (raises SystemExit, triggers finally blocks).
signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR         = Path(__file__).resolve().parent
HEEPSILON_CORE     = SCRIPT_DIR / "heepsilon.core"
CONFIGS_DIR        = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/configs"
HEEPSILON_APPS_DIR = SCRIPT_DIR / "sw/applications"
SATMAPIT_APPS_DIR  = SCRIPT_DIR / "sw/satmapit"
XHEEP_APPS_DIR     = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/sw/applications"
SW_BUILD           = SCRIPT_DIR / "sw/build"
DEFAULT_CONDA      = "core-v-mini-mcu"

# ── CGRA grid configuration ───────────────────────────────────────────────────
# Which grid the checked-out generated RTL/SW belong to, written by `make
# mcu-gen` (see Makefile CGRA_CFG).  Each grid owns a separate FuseSoC build
# root, so simulators and bitstreams for different grids coexist.

def available_cgra_cfgs() -> list:
    return sorted(p.stem.replace("heepsilon_cfg_", "")
                  for p in (SCRIPT_DIR / "cfg").glob("heepsilon_cfg_*.hjson"))

def _active_cgra_cfg() -> str:
    # --cgra-cfg is resolved here, before BUILD_ROOT and BOARD_CONFIG are built
    # from it, so every path in this module belongs to the requested grid.
    # argparse runs too late for that.
    for i, a in enumerate(sys.argv):
        if a == "--cgra-cfg" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--cgra-cfg="):
            return a.split("=", 1)[1]
    try:
        return (SCRIPT_DIR / ".heepsilon_active_cfg").read_text().strip() or "4x4"
    except OSError:
        return "4x4"

def _stamped_cgra_cfg() -> Optional[str]:
    """Grid the checked-out generated files belong to, None if never generated."""
    try:
        return (SCRIPT_DIR / ".heepsilon_active_cfg").read_text().strip() or None
    except OSError:
        return None

CGRA_CFG = _active_cgra_cfg()
# 4x4 keeps FuseSoC's historical default path.
BUILD_ROOT = SCRIPT_DIR / (
    "build/eslepfl_systems_heepsilon_0" if CGRA_CFG == "4x4"
    else f"build/heepsilon_{CGRA_CFG}"
)

# ── Colour helpers ─────────────────────────────────────────────────────────────

RESET  = "\033[0m"
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"

def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text

def info(msg: str)   -> None: print(_c(YELLOW, "[INFO]"), msg)
def ok(msg: str)     -> None: print(_c(GREEN,  "[PASS]"), msg)
def err(msg: str)    -> None: print(_c(RED,    "[FAIL]"), msg, file=sys.stderr)
def header(msg: str) -> None: print(_c(BOLD, msg))

_SPIN_FRAMES = ["|", "/", "-", "\\"]

@contextmanager
def _spinning(message: str):
    """Show an animated spinner on TTY while the body runs; plain info line otherwise."""
    if not sys.stdout.isatty():
        info(message + " ...")
        yield
        return
    done  = threading.Event()
    def _spin() -> None:
        for frame in itertools.cycle(_SPIN_FRAMES):
            if done.is_set():
                break
            sys.stdout.write(f"\r  {_c(CYAN, frame)} {message} ...")
            sys.stdout.flush()
            time.sleep(0.12)
    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
        t.join()
        sys.stdout.write(f"\r  {_c(GREEN, '✓')} {message}    \n")
        sys.stdout.flush()

# ── Simulator targets ─────────────────────────────────────────────────────────

SIM_TARGETS = {
    "verilator": {
        "run_target":   "run-verilator",
        "build_target": "verilator-sim",
        "log_dir":      "sim-verilator",
    },
    "questasim": {
        "run_target":   "run-questasim",
        "build_target": "questasim-sim",
        "log_dir":      "sim-modelsim",
    },
}
DEFAULT_SIMULATOR = "verilator"

def sim_log_dir(simulator: str = DEFAULT_SIMULATOR) -> Path:
    return BUILD_ROOT / SIM_TARGETS[simulator]["log_dir"]

def _sim_built(simulator: str) -> bool:
    return sim_log_dir(simulator).is_dir()

# ── Board configurations ──────────────────────────────────────────────────────

DEFAULT_BOARD = "pynq-z2"
UART_IDLE_TIMEOUT = 30  # seconds

BOARD_CONFIG: dict[str, dict] = {
    "pynq-z1": {
        "vid_pid":     "0403:6010",
        "bitstream":   BUILD_ROOT / "pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit",
        "openocd_cfg": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z2-bscan.cfg",
        "program_tcl": SCRIPT_DIR / "program_fpga.tcl",
        "uart_search": ["CP2102", "Silicon_Labs"],
        "baud": 9600,
        "linker": "on_chip",
    },
    "pynq-z2": {
        "vid_pid":     "0403:6010",
        "bitstream":   BUILD_ROOT / "pynq-z2-vivado/eslepfl_systems_heepsilon_0.bit",
        "openocd_cfg": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z2-bscan.cfg",
        "program_tcl": SCRIPT_DIR / "program_fpga.tcl",
        "uart_search": ["CP2102", "Silicon_Labs"],
        "baud": 9600,
        "linker": "on_chip",
    },
    "zcu104": {
        "vid_pid":         "0403:6011",
        "bitstream":       BUILD_ROOT / "zcu104-vivado/eslepfl_systems_heepsilon_0.bit",
        "openocd_cfg":     SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-zcu104-bscan.cfg",
        "openocd_cfg_ext": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-zcu104-ext-jtag.cfg",
        "program_tcl":     SCRIPT_DIR / "program_fpga_zcu104.tcl",
        "uart_search":     [["Xilinx_JTAG+3Serial", "if03"]],
        "baud": 9600,
        "linker": "on_chip",
    },
}

# ── Test definitions ───────────────────────────────────────────────────────────

@dataclass
class TestCase:
    app:          str
    pass_pattern: str        # regex checked against sim uart0.log or FPGA UART capture
    fail_pattern: str = ""   # regex — explicit fail trigger in sim
    description:  str = ""
    enabled:      bool = True
    skip_reason:  str = ""
    sentinel:     str = ""   # plain-string UART stop trigger for FPGA capture

    def fpga_sentinel(self) -> str:
        return self.sentinel if self.sentinel else "### DONE ###"

# Apps with known UART output patterns. Everything else is auto-discovered.
_KNOWN_TESTS: list[TestCase] = [
    # ── X-HEEP apps ─────────────────────────────────────────────────────────────
    TestCase("hello_world",              r"hello world",
             sentinel="hello world!",
             description="X-HEEP smoke test"),
    # ── HEEPsilon CGRA apps ──────────────────────────────────────────────────────
    TestCase("cgra_func_test",           r"finished with 0 errors",
             sentinel="finished with",
             description="CGRA functionality check"),
    TestCase("cgra_load_store_test",     r"finished with 0 errors",
             sentinel="functionality check finished with",
             description="CGRA load/store check"),
    TestCase("cgra_alu_test",            r"finished with 0 errors",
             sentinel="finished with",
             description="CGRA ALU operation coverage"),
    TestCase("cgra_leftright_test",      r"finished with 0 errors",
             sentinel="finished with",
             description="CGRA inter-column data passing via RCL"),
    TestCase("cgra_fullgrid_test",       r"finished with 0 errors",
             sentinel="finished with",
             description="CGRA full 4×4 grid"),
    TestCase("cgra_fft",                 r"finished with 0 errors",
             sentinel="FFT computation finished with",
             description="CGRA FFT computation"),
    TestCase("kernel_test",              r"E\t0",
             sentinel="E\t",
             description="Multi-kernel CGRA benchmark"),
    TestCase("cgra_dbl_search",          r"finished with 0 errors",
             sentinel="finished with",
             description="CGRA double min/max search"),
    TestCase("cgra_check_conf",          r"finished with",
             sentinel="CGRA configuration check finished with",
             description="CGRA configuration register check"),
    TestCase("cgra_cpu_parallel",        r"### DONE ###",
             description="CGRA+CPU parallel execution"),
    TestCase("cgra_fir",                 r"finished with",
             sentinel="CGRA FIR finished with",
             description="CGRA FIR filter"),
    TestCase("cgra_loop_preempt",        r"### DONE ###",
             description="CGRA kernel preemption via loop"),
    TestCase("cgra_reversebits",         r"### DONE ###",
             description="CGRA bit-reversal (SAT-MapIt pipeline)"),
    TestCase("cgra_sad",                 r"### DONE ###",
             description="CGRA sum of absolute differences"),
    TestCase("cgra_xorshifthash",        r"finished with",
             sentinel="CGRA xorshifthash finished with",
             description="CGRA XorShift hash kernel"),
    TestCase("mmul_os",                  r"Total cgra:",
             sentinel="Total cgra:",
             description="CGRA output-stationary matrix multiply"),
    TestCase("transformer",              r"END",
             sentinel="END",
             description="CGRA transformer inference"),
    TestCase("transformer_without_cgra", r".",
             enabled=False, skip_reason="no main.c (SYLT-FFT subdir only)",
             description="CPU-only transformer — missing main.c"),
    TestCase("trans_versasense",         r"Distances",
             sentinel="Distances",
             description="Transformer VersaSense demo"),
    # rgb_led has 2M-cycle spin-delays; it finishes but is slow in sim.
    TestCase("rgb_led",                  r"### DONE ###",
             description="RGB LED GPIO demo (slow sim — use --timeout)"),
]

def _discover_tests() -> list[TestCase]:
    known  = {t.app: t for t in _KNOWN_TESTS}
    result = list(_KNOWN_TESTS)
    seen   = set(known)

    for path in sorted(HEEPSILON_APPS_DIR.iterdir()):
        if path.is_dir() and path.name not in seen:
            result.append(TestCase(path.name, r".", description="(heepsilon app)"))
            seen.add(path.name)

    if SATMAPIT_APPS_DIR.exists():
        for path in sorted(SATMAPIT_APPS_DIR.iterdir()):
            if path.is_dir() and path.name not in seen:
                result.append(TestCase(path.name, r".", description="(satmapit app)"))
                seen.add(path.name)

    for path in sorted(XHEEP_APPS_DIR.iterdir()):
        if path.is_dir() and path.name not in seen:
            result.append(TestCase(path.name, r".",
                                   description="(x-heep app)",
                                   enabled=False,
                                   skip_reason="not in default CI run"))
            seen.add(path.name)

    return result

TESTS = _discover_tests()
TESTS_BY_NAME: dict[str, TestCase] = {t.app: t for t in TESTS}

# ── Build configurations (auto-discovered from configs/) ───────────────────────

# Bank count must match the bitstream on the FPGA, otherwise the linker script
# places sections at addresses that don't exist → silent crash, no UART.
# None = let the Makefile pick the per-CGRA_CFG default (4x4: 6, others: 12);
# --memory-banks overrides it.
HEEPSILON_DEFAULT_MEMORY_BANKS = None

@dataclass
class BuildConfig:
    name:         str
    x_heep_cfg:   str
    bank_size_kb: int   # single-bank size from hjson (informational only)
    description:  str

def _discover_configs() -> list[BuildConfig]:
    configs = []
    for path in sorted(CONFIGS_DIR.glob("*.hjson")):
        text = path.read_text(errors="replace")
        if "code_and_data" not in text:
            continue
        # Skip configs that declare data_interleaved — they require MEMORY_BANKS_IL
        # and a higher total bank count than code_and_data.num alone.
        if "data_interleaved" in text:
            continue
        m = re.search(
            r"code_and_data\s*:\s*\{[^}]*\bnum\s*:\s*(\d+)[^}]*\bsizes\s*[=:]\s*[\[\s]*(\d+)",
            text, re.DOTALL,
        )
        if not m:
            continue
        num_in_cfg, size_kb = int(m.group(1)), int(m.group(2))
        configs.append(BuildConfig(
            path.stem, f"configs/{path.name}", size_kb,
            f"bank size {size_kb} KB  ({num_in_cfg} in hjson, actual count via --memory-banks)",
        ))
    return configs

BUILD_CONFIGS: list[BuildConfig]            = _discover_configs()
BUILD_CONFIGS_BY_NAME: dict[str, BuildConfig] = {c.name: c for c in BUILD_CONFIGS}

# Use general.hjson as the hjson template (no data_interleaved issues).
# MEMORY_BANKS is controlled separately (see HEEPSILON_DEFAULT_MEMORY_BANKS).
DEFAULT_CONFIG: str = (
    "cgra_fat" if "cgra_fat" in BUILD_CONFIGS_BY_NAME
    else "general" if "general" in BUILD_CONFIGS_BY_NAME
    else BUILD_CONFIGS[0].name if BUILD_CONFIGS else "general"
)

# ── Environment ────────────────────────────────────────────────────────────────

_env_cache: Optional[dict[str, str]] = None

def load_env() -> dict[str, str]:
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    info("Sourcing env.sh")
    result = subprocess.run(
        ["bash", "-c", f"source {SCRIPT_DIR}/env.sh && env -0"],
        capture_output=True, text=True, cwd=SCRIPT_DIR,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to source env.sh:\n{result.stderr}")
    env: dict[str, str] = {}
    for item in result.stdout.split("\0"):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    _env_cache = env
    return env

# ── Command runner ─────────────────────────────────────────────────────────────

def run_cmd(
    cmd: list[str],
    *,
    conda_env: str = DEFAULT_CONDA,
    dry_run:   bool = False,
    timeout:   Optional[int] = None,
    capture:   bool = False,
) -> subprocess.CompletedProcess:
    full_cmd = ["conda", "run", "--no-capture-output", "-n", conda_env] + cmd
    if dry_run:
        print(_c(CYAN, f"  [dry-run] {' '.join(full_cmd)}"))
        return subprocess.CompletedProcess(full_cmd, 0, "", "")
    kwargs: dict = dict(env=load_env(), cwd=SCRIPT_DIR)
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    try:
        return subprocess.run(full_cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        err(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        return subprocess.CompletedProcess(full_cmd, -1, "", "timeout")

# ── Patch steps ───────────────────────────────────────────────────────────────

def apply_patches(dry_run: bool = False) -> None:
    text = HEEPSILON_CORE.read_text()
    if "Wno-UNDRIVEN" not in text:
        info("Patching heepsilon.core: adding -Wno-UNDRIVEN")
        if not dry_run:
            HEEPSILON_CORE.write_text(
                text.replace(
                    '          - "-Wall"',
                    '          - "-Wall"\n          - "-Wno-UNDRIVEN"',
                )
            )
    else:
        info("heepsilon.core: -Wno-UNDRIVEN already present")

# ── Build steps ────────────────────────────────────────────────────────────────

def ensure_python_deps(dry_run: bool = False) -> None:
    info("Checking Python deps (hjson mako jsonref)")
    try:
        import hjson, mako, jsonref  # noqa: F401
        info("All Python deps present")
    except ImportError:
        info("Installing missing deps via pip")
        run_cmd(["pip", "install", "-q", "hjson", "mako", "jsonref"], dry_run=dry_run)

def mcu_gen(
    dry_run:      bool = False,
    conda_env:    str  = DEFAULT_CONDA,
    config:       Optional[BuildConfig] = None,
    memory_banks: Optional[int] = HEEPSILON_DEFAULT_MEMORY_BANKS,
) -> None:
    cfg = config or BUILD_CONFIGS_BY_NAME[DEFAULT_CONFIG]
    banks = f"{memory_banks}" if memory_banks else "<Makefile default>"
    info(f"Running mcu-gen  (X_HEEP_CFG={cfg.x_heep_cfg}  MEMORY_BANKS={banks}  "
         f"CGRA_CFG={CGRA_CFG})  [{cfg.name}]")
    cmd = ["make", "mcu-gen", f"X_HEEP_CFG={cfg.x_heep_cfg}", f"CGRA_CFG={CGRA_CFG}"]
    if memory_banks:
        cmd.append(f"MEMORY_BANKS={memory_banks}")
    r = run_cmd(cmd, conda_env=conda_env, dry_run=dry_run)
    if r.returncode != 0:
        sys.exit("mcu-gen failed — aborting.")

def build_sim(
    dry_run:   bool = False,
    conda_env: str  = DEFAULT_CONDA,
    simulator: str  = DEFAULT_SIMULATOR,
) -> None:
    target = SIM_TARGETS[simulator]["build_target"]
    info(f"Building {simulator} simulator  ({target})")
    r = run_cmd(["make", target, f"CGRA_CFG={CGRA_CFG}"], conda_env=conda_env, dry_run=dry_run)
    if r.returncode != 0:
        sys.exit(f"{target} build failed — aborting.")

def build_app(
    app:           str,
    board:         str = "sim",
    force_on_chip: bool = False,
    dry_run:       bool = False,
) -> None:
    linker = "on_chip" if (board == "sim" or force_on_chip) else BOARD_CONFIG[board].get("linker", "on_chip")
    target = "sim" if board == "sim" else board
    header(f"Building {app}  (linker={linker}  target={target})")
    app_dir = "satmapit" if (SATMAPIT_APPS_DIR / app).is_dir() else "applications"
    r = run_cmd(["make", "app", f"PROJECT={app}", f"APP_DIR={app_dir}", f"CGRA_CFG={CGRA_CFG}",
                 f"LINKER={linker}", f"TARGET={target}"],
                dry_run=dry_run)
    if not dry_run and r.returncode != 0:
        sys.exit(f"make app failed for {app}.")
    ok(f"{app} compiled")

# ── Sim test runner ────────────────────────────────────────────────────────────

@dataclass
class Result:
    app:    str
    passed: bool
    reason: str = ""
    log:    str = ""
    config: str = ""
    target: str = "sim"

def run_sim_test(
    tc:        TestCase,
    *,
    verbose:   bool = False,
    dry_run:   bool = False,
    timeout:   Optional[int] = None,
    save_logs: Optional[Path] = None,
    conda_env: str = DEFAULT_CONDA,
    repeat:    int = 1,
    config:    Optional[BuildConfig] = None,
    simulator: str = DEFAULT_SIMULATOR,
) -> Result:
    cfg_name   = config.name if config else ""
    cfg_tag    = f"[{cfg_name}] " if cfg_name else ""
    run_target = SIM_TARGETS[simulator]["run_target"]
    log_path   = sim_log_dir(simulator) / "uart0.log"

    for attempt in range(1, repeat + 1):
        tag = f" (attempt {attempt}/{repeat})" if repeat > 1 else ""
        info(f"{cfg_tag}Running {tc.app}{tag} ...")

        if log_path.exists():
            log_path.unlink()

        app_dir = "satmapit" if (SATMAPIT_APPS_DIR / tc.app).is_dir() else "applications"
        r = run_cmd(
            ["make", run_target, f"PROJECT={tc.app}", f"APP_DIR={app_dir}",
             f"CGRA_CFG={CGRA_CFG}"],
            conda_env=conda_env, dry_run=dry_run, timeout=timeout,
        )

        if dry_run:
            return Result(tc.app, True, "dry-run", config=cfg_name)

        if r.returncode == -1:
            result = Result(tc.app, False, f"timeout after {timeout}s", config=cfg_name)
            if attempt == repeat:
                err(f"{cfg_tag}{tc.app} — {result.reason}")
            continue

        if r.returncode != 0:
            result = Result(tc.app, False, "make error", config=cfg_name)
            if attempt == repeat:
                err(f"{cfg_tag}{tc.app} — make exited non-zero")
            continue

        if not log_path.exists():
            result = Result(tc.app, False, "no uart0.log produced", config=cfg_name)
            if attempt == repeat:
                err(f"{cfg_tag}{tc.app} — no uart0.log produced")
            continue

        log = log_path.read_text(errors="replace")

        if save_logs:
            save_logs.mkdir(parents=True, exist_ok=True)
            stem = "_".join(filter(None, [tc.app, cfg_name, f"attempt{attempt}" if repeat > 1 else ""]))
            (save_logs / f"{stem}.log").write_text(log)

        if re.search(r"Out of bound memory access|\$stop", log):
            result = Result(tc.app, False, "sim abort", log, cfg_name)
            if attempt == repeat:
                err(f"{cfg_tag}{tc.app} — simulation aborted (out-of-bounds / $stop)")
                if verbose:
                    print(log)
            continue

        if tc.fail_pattern and re.search(tc.fail_pattern, log):
            result = Result(tc.app, False, "fail pattern matched", log, cfg_name)
            if attempt == repeat:
                err(f"{cfg_tag}{tc.app} — fail pattern matched")
                if verbose:
                    print(log)
            continue

        if re.search(tc.pass_pattern, log):
            ok(f"{cfg_tag}{tc.app}" + (f" (passed on attempt {attempt})" if attempt > 1 else ""))
            if verbose:
                print(log)
            return Result(tc.app, True, "", log, cfg_name)

        result = Result(tc.app, False, "pass pattern not found", log, cfg_name)
        if attempt == repeat:
            err(f"{cfg_tag}{tc.app} — pass pattern not found in uart0.log")
            print(log)

    return result  # type: ignore[return-value]

def run_sim_matrix(
    tests:     list[TestCase],
    configs:   list[BuildConfig],
    *,
    opts:      dict,
    conda_env: str = DEFAULT_CONDA,
    simulator: str = DEFAULT_SIMULATOR,
) -> list[Result]:
    all_results: list[Result] = []
    for cfg in configs:
        header(f"\n  ── Config: {cfg.name}  ({cfg.description}) ──\n")
        if opts.get("run_gen"):
            mcu_gen(opts.get("dry_run", False), conda_env, cfg,
                    memory_banks=opts.get("memory_banks", HEEPSILON_DEFAULT_MEMORY_BANKS))
        if opts.get("run_build"):
            build_sim(opts.get("dry_run", False), conda_env, simulator)
        if not opts.get("dry_run") and not _sim_built(simulator):
            err(f"Simulator not built ({SIM_TARGETS[simulator]['log_dir']}/ missing) — skipping config {cfg.name}")
            continue
        for tc in tests:
            r = run_sim_test(
                tc,
                verbose   = opts.get("verbose", False),
                dry_run   = opts.get("dry_run", False),
                timeout   = opts.get("timeout"),
                save_logs = opts.get("save_logs"),
                conda_env = conda_env,
                repeat    = opts.get("repeat") or 1,
                config    = cfg,
                simulator = simulator,
            )
            all_results.append(r)
            if opts.get("fail_fast") and not r.passed:
                err(f"Stopping early (fail-fast) at config={cfg.name}")
                return all_results
    return all_results

# ── Result reporting ───────────────────────────────────────────────────────────

def print_summary(results: list[Result]) -> bool:
    print()
    header("══════════════════════════════════════")
    header("           TEST SUMMARY               ")
    header("══════════════════════════════════════")
    overall = True
    for r in results:
        tag   = f"[{r.target}] " if r.target != "sim" else ""
        label = f"{tag}[{r.config}] {r.app}" if r.config else f"{tag}{r.app}"
        if r.passed:
            ok(label)
        else:
            err(f"{label}  ({r.reason})")
            overall = False
    header("══════════════════════════════════════")
    return overall

def write_json_report(results: list[Result], path: Path) -> None:
    def _entry(r: Result) -> dict:
        e: dict = {"app": r.app, "passed": r.passed, "reason": r.reason}
        if r.config:
            e["config"] = r.config
        if r.target != "sim":
            e["target"] = r.target
        return e
    report = {
        "timestamp": datetime.now().isoformat(),
        "overall":   all(r.passed for r in results),
        "results":   [_entry(r) for r in results],
    }
    path.write_text(json.dumps(report, indent=2))
    info(f"JSON report written to {path}")

# ── FPGA steps ─────────────────────────────────────────────────────────────────

def check_board(board: str) -> None:
    header("Board detection")
    config = BOARD_CONFIG.get(board)
    if not config:
        sys.exit(f"Unknown board: {board}. Available: {', '.join(BOARD_CONFIG)}")
    result = subprocess.run(["lsusb"], capture_output=True, text=True)
    if config["vid_pid"] not in result.stdout:
        sys.exit(
            f"{board} not found (USB VID:PID {config['vid_pid']} not detected).\n"
            "Check that the board is powered and connected via USB-JTAG."
        )
    ok(f"{board} detected (VID:PID {config['vid_pid']})")

def _bitstream_fresh(board: str) -> bool:
    b = BOARD_CONFIG[board]["bitstream"]
    return b.exists() and b.stat().st_size > 100_000

def check_bitstream(board: str) -> None:
    header("Bitstream check")
    if _bitstream_fresh(board):
        ok(f"Bitstream ready: {BOARD_CONFIG[board]['bitstream'].name}")
        return
    info(f"Bitstream not found: {BOARD_CONFIG[board]['bitstream']}")
    answer = input("Rebuild bitstream now? (takes ~20 min) [y/N] ").strip().lower()
    if not answer.startswith("y"):
        sys.exit("Bitstream not available — aborting.")
    header("Building FPGA bitstream")
    cmd = run_cmd(["make", "vivado-fpga", f"FPGA_BOARD={board}", f"CGRA_CFG={CGRA_CFG}",
                   "FUSESOC_FLAGS=--flag=use_bscane_xilinx"])
    if not _bitstream_fresh(board):
        sys.exit("Build completed but bitstream still not valid — check buildvivado.log.")
    ok("Bitstream built successfully")

def program_bitstream(board: str) -> None:
    header("Programming bitstream")
    config = BOARD_CONFIG[board]
    with _spinning("Running Vivado batch-mode programmer"):
        result = subprocess.run(
            ["vivado", "-nolog", "-nojournal", "-mode", "batch",
             "-source", str(config["program_tcl"])],
            env=load_env(), cwd=SCRIPT_DIR, capture_output=True, text=True,
        )
    combined = result.stdout + result.stderr
    if "End of startup status: HIGH" not in combined:
        print(combined)
        sys.exit("Programming failed — 'End of startup status: HIGH' not found.")
    ok(f"Bitstream programmed for {board}")
    with _spinning("Waiting for FPGA/USB to stabilise"):
        time.sleep(2)

def _drain_stdout(proc: subprocess.Popen, prefix: str) -> None:
    try:
        for line in proc.stdout:
            print(f"  [{prefix}] {line}", end="")
    except Exception:
        pass

def _kill_stale_openocd() -> None:
    """Kill any running openocd processes to avoid LIBUSB_ERROR_BUSY."""
    result = subprocess.run(["pgrep", "-x", "openocd"], capture_output=True, text=True)
    pids = result.stdout.split()
    if pids:
        info(f"Killing stale OpenOCD PID(s): {' '.join(pids)}")
        subprocess.run(["kill"] + pids, capture_output=True)
        time.sleep(1)

def _register_openocd_cleanup(proc: subprocess.Popen) -> None:
    """Ensure proc is killed even on abrupt exit (atexit fires on sys.exit/SIGTERM)."""
    def _cleanup() -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    atexit.register(_cleanup)

def _openocd_start_proc(board: str, ext_jtag: bool) -> subprocess.Popen:
    config = BOARD_CONFIG[board]
    cfg = (config.get("openocd_cfg_ext") if ext_jtag else None) or config["openocd_cfg"]
    return subprocess.Popen(
        ["openocd", "-f", str(cfg)],
        env=load_env(), cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

def _wait_openocd_ready(
    proc:    subprocess.Popen,
    prefix:  str,
    timeout: int,
    fatal:   bool = True,
) -> bool:
    # daemon thread prevents readline() from blocking past the deadline
    q: queue.Queue[Optional[str]] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)  # EOF sentinel

    threading.Thread(target=_reader, daemon=True).start()

    tty      = sys.stdout.isatty()
    spin_it  = itertools.cycle(_SPIN_FRAMES)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if tty:
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.flush()
            if fatal:
                proc.terminate()
                sys.exit(f"OpenOCD did not become ready within {timeout} s.")
            info(f"[{prefix}] No response within {timeout} s — FPGA not configured")
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            return False
        try:
            line = q.get(timeout=min(remaining, 0.25))
        except queue.Empty:
            if proc.poll() is not None:
                if tty:
                    sys.stdout.write("\r" + " " * 40 + "\r")
                    sys.stdout.flush()
                msg = f"[{prefix}] OpenOCD exited (rc={proc.returncode})"
                if fatal:
                    sys.exit(msg)
                info(msg + " — FPGA not responding")
                return False
            if tty:
                elapsed = timeout - remaining
                frame   = next(spin_it)
                sys.stdout.write(f"\r  {_c(CYAN, frame)} [{prefix}] waiting ... {elapsed:.0f}s")
                sys.stdout.flush()
            continue
        if tty:
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()
        if line is None:  # EOF
            msg = f"[{prefix}] OpenOCD stdout closed"
            if fatal:
                sys.exit(msg)
            info(msg + " — FPGA not responding")
            return False
        print(f"  [{prefix}] {line}", end="")
        if "Ready for Remote Connections" in line:
            return True

def start_openocd(board: str, ext_jtag: bool = False) -> subprocess.Popen:
    header("Starting OpenOCD")
    _kill_stale_openocd()
    proc = _openocd_start_proc(board, ext_jtag)
    info(f"OpenOCD PID {proc.pid} — waiting for 'Ready for Remote Connections' ...")
    _wait_openocd_ready(proc, "openocd", timeout=60, fatal=True)
    ok("OpenOCD ready")
    _register_openocd_cleanup(proc)
    time.sleep(0.5)
    threading.Thread(target=_drain_stdout, args=(proc, "openocd"), daemon=True).start()
    return proc

def _probe_openocd(board: str, ext_jtag: bool = False, timeout: int = 10) -> Optional[subprocess.Popen]:
    """
    Probe whether the FPGA already has a bitstream loaded.
    Returns a running OpenOCD Popen on success, or None if no response within timeout.
    """
    _kill_stale_openocd()
    proc = _openocd_start_proc(board, ext_jtag)
    info(f"[probe] OpenOCD PID {proc.pid} — waiting up to {timeout} s ...")
    if not _wait_openocd_ready(proc, "probe", timeout=timeout, fatal=False):
        return None
    ok("FPGA already configured — skipping bitstream programming")
    _register_openocd_cleanup(proc)
    time.sleep(0.5)
    threading.Thread(target=_drain_stdout, args=(proc, "openocd"), daemon=True).start()
    return proc

def find_uart(board: str) -> str:
    header("Locating UART port")
    config = BOARD_CONFIG[board]
    by_id = Path("/dev/serial/by-id")
    if by_id.exists():
        for entry in by_id.iterdir():
            name = entry.name
            for pattern in config["uart_search"]:
                terms = [pattern] if isinstance(pattern, str) else pattern
                if all(t in name for t in terms):
                    port = str(entry.resolve())
                    ok(f"UART found: {port}  (via {entry})")
                    return port
    info(f"Automatic UART detection failed for {board}.")
    print("Available serial ports:")
    for p in sorted(Path("/dev").glob("ttyUSB*")):
        print(f"  - {p}")
    port = input("Enter UART port manually (e.g. /dev/ttyUSB1): ").strip()
    if not port:
        sys.exit("No UART port specified.")
    return port

def open_uart(port: str, board: str):
    try:
        import serial
    except ImportError:
        sys.exit("pyserial not installed. Run: pip install pyserial")
    baud = BOARD_CONFIG[board]["baud"]
    ser  = serial.Serial(port, baud, timeout=0.1)
    ok(f"UART opened: {port} @ {baud} baud")
    return ser

def _stop_gdb(proc: subprocess.Popen) -> None:
    # SIGINT halts the remote target first; disconnect/quit are then accepted cleanly
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)   # halts remote target via GDB's Ctrl-C handler
        time.sleep(0.3)
        proc.stdin.write("disconnect\nquit\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

def load_via_gdb(reset_first: bool = True) -> subprocess.Popen:
    header("Loading via GDB")
    elf = SW_BUILD / "main.elf"
    if not elf.exists():
        sys.exit(f"main.elf not found at {elf}")
    proc = subprocess.Popen(
        ["gdb-multiarch", str(elf)],
        env=load_env(), cwd=SCRIPT_DIR,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    def _send(cmd: str) -> None:
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
    info(f"GDB PID {proc.pid}")
    time.sleep(0.3)
    _send("set remotetimeout 2000")
    _send("target remote localhost:3333")
    if reset_first:
        _send("monitor reset halt")
    _send("load")
    _send("continue")
    threading.Thread(target=_drain_stdout, args=(proc, "gdb"), daemon=True).start()
    ok("GDB: load + continue sent")
    return proc

def capture_uart(ser, sentinel: str, idle_timeout: int = UART_IDLE_TIMEOUT) -> str:
    info(f"Capturing UART  (sentinel: {sentinel!r}  idle timeout: {idle_timeout}s)")
    buf: list[str] = []
    done  = threading.Event()
    chars: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        try:
            while not done.is_set():
                data = ser.read(256)
                if data:
                    chars.put(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    line_buf   = ""
    last_recv  = time.monotonic()
    try:
        while True:
            try:
                chunk = chars.get(timeout=0.1)
                last_recv = time.monotonic()
                sys.stdout.write(chunk)
                sys.stdout.flush()
                buf.append(chunk)
                line_buf += chunk
                if sentinel in line_buf:
                    break
            except queue.Empty:
                if time.monotonic() - last_recv > idle_timeout:
                    info(f"Idle timeout ({idle_timeout}s) — stopping capture")
                    break
    finally:
        done.set()
    return "".join(buf)

# ── Output comparison ──────────────────────────────────────────────────────────

_CYCLE_PATTERNS = (
    "active cycles", "stall cycles", "CGRA kernel executed", "spent_cy", "cycles:",
)

def _normalise(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").splitlines():
        line = line.rstrip()
        if line and not any(p in line for p in _CYCLE_PATTERNS):
            out.append(line)
    return out

def compare_outputs(fpga_out: str, sim_out: str, app: str = "") -> bool:
    header(f"FPGA vs sim comparison{f' — {app}' if app else ''}")
    info("Cycle-count lines excluded (expected to differ between sim and HW)")
    fpga_lines = _normalise(fpga_out)
    sim_lines  = _normalise(sim_out)
    max_len    = max(len(fpga_lines), len(sim_lines), 1)
    matches = mismatches = 0
    for i in range(max_len):
        fl = fpga_lines[i] if i < len(fpga_lines) else "<missing>"
        sl = sim_lines[i]  if i < len(sim_lines)  else "<missing>"
        if fl == sl:
            matches += 1
        else:
            mismatches += 1
            print(_c(RED,    f"  line {i+1:3d} MISMATCH"))
            print(_c(RED,    f"    FPGA : {fl!r}"))
            print(_c(YELLOW, f"    SIM  : {sl!r}"))
    print()
    ok(f"Matching lines : {matches}")
    if mismatches:
        err(f"Mismatching lines: {mismatches}")
    else:
        ok("All lines match")
    return mismatches == 0

# ── FPGA session (single board session for one or many apps) ───────────────────

def run_fpga_session(
    tests:        list[TestCase],
    board:        str,
    *,
    skip_program: bool = False,
    force_program: bool = False,
    ext_jtag:     bool = False,
    uart_timeout: int  = UART_IDLE_TIMEOUT,
    dry_run:      bool = False,
    save_logs:    Optional[Path] = None,
    sim_verify:   bool = False,
    simulator:    str  = DEFAULT_SIMULATOR,
    verbose:      bool = False,
) -> list[Result]:
    # OpenOCD stays alive across all apps; GDB is restarted per app for a clean CPU state
    results: list[Result] = []
    openocd_proc: Optional[subprocess.Popen] = None
    gdb_proc:     Optional[subprocess.Popen] = None
    ser = None

    try:
        if not dry_run:
            check_board(board)

            if force_program:
                check_bitstream(board)
                program_bitstream(board)
                openocd_proc = start_openocd(board, ext_jtag=ext_jtag)
            elif skip_program:
                openocd_proc = start_openocd(board, ext_jtag=ext_jtag)
            else:
                info("Auto-detecting bitstream: probing FPGA ...")
                openocd_proc = _probe_openocd(board, ext_jtag=ext_jtag)
                if openocd_proc is None:
                    info("FPGA not configured — programming bitstream")
                    check_bitstream(board)
                    program_bitstream(board)
                    openocd_proc = start_openocd(board, ext_jtag=ext_jtag)
            uart_port    = find_uart(board)
            ser          = open_uart(uart_port, board)

        for tc in tests:
            info(f"Running {tc.app} on {board} ...")

            if dry_run:
                results.append(Result(tc.app, True, "dry-run", target="fpga"))
                continue

            build_app(tc.app, board)
            gdb_proc = load_via_gdb(reset_first=True)

            header(f"Capturing UART output — {tc.app}")
            fpga_output = capture_uart(ser, tc.fpga_sentinel(), uart_timeout)
            print()

            _stop_gdb(gdb_proc)
            gdb_proc = None

            if verbose:
                print(fpga_output)
            if save_logs:
                save_logs.mkdir(parents=True, exist_ok=True)
                (save_logs / f"{tc.app}_fpga.log").write_text(fpga_output)

            passed = bool(re.search(tc.pass_pattern, fpga_output))
            reason = "" if passed else "pass pattern not found in FPGA UART output"
            if passed:
                ok(f"{tc.app} — FPGA PASS")
            else:
                err(f"{tc.app} — FPGA FAIL")
            results.append(Result(tc.app, passed, reason, fpga_output, target="fpga"))

            if sim_verify:
                header(f"Sim verification — {tc.app}")
                sim_out = _sim_output_for(tc, simulator)
                if sim_out:
                    compare_outputs(fpga_output, sim_out, tc.app)

    finally:
        if gdb_proc is not None:
            _stop_gdb(gdb_proc)
        if openocd_proc is not None and openocd_proc.poll() is None:
            info("Terminating OpenOCD")
            openocd_proc.terminate()
            try:
                openocd_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                openocd_proc.kill()
        if ser is not None:
            try:
                ser.close()
                info("Serial port closed")
            except Exception:
                pass

    return results

def _sim_output_for(tc: TestCase, simulator: str = DEFAULT_SIMULATOR) -> str:
    log_path = sim_log_dir(simulator) / "uart0.log"
    if log_path.exists():
        log_path.unlink()
    run_target = SIM_TARGETS[simulator]["run_target"]
    app_dir = "satmapit" if (SATMAPIT_APPS_DIR / tc.app).is_dir() else "applications"
    r = run_cmd(["make", run_target, f"PROJECT={tc.app}", f"APP_DIR={app_dir}", f"CGRA_CFG={CGRA_CFG}"])
    if r.returncode != 0:
        err(f"Sim run failed for {tc.app} — skipping comparison")
        return ""
    if log_path.exists():
        return log_path.read_text(errors="replace")
    err("uart0.log not found after simulation")
    return ""

# ── Interactive menu helpers ───────────────────────────────────────────────────

def _pick(prompt: str, options: list[str]) -> int:
    while True:
        print()
        header(prompt)
        for i, opt in enumerate(options, 1):
            print(f"  {_c(CYAN, str(i))}. {opt}")
        raw = input("\nChoice: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(_c(RED, "  Invalid — enter a number from the list."))

def _multi_pick(prompt: str, options: list[str]) -> list[int]:
    print()
    header(prompt)
    print(_c(YELLOW, "  Space- or comma-separated numbers, or Enter to select all."))
    for i, opt in enumerate(options, 1):
        print(f"  {_c(CYAN, str(i))}. {opt}")
    raw = input("\nSelection: ").strip()
    if not raw:
        return list(range(len(options)))
    indices = []
    for tok in re.split(r"[\s,]+", raw):
        if tok.isdigit() and 1 <= int(tok) <= len(options):
            indices.append(int(tok) - 1)
    return indices if indices else list(range(len(options)))

def _ask_bool(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    raw  = input(f"{prompt} {hint} ").strip().lower()
    return (raw.startswith("y") if raw else default)

def _ask_int(prompt: str, default: Optional[int] = None) -> Optional[int]:
    hint = f" (default: {default})" if default is not None else ""
    raw  = input(f"{prompt}{hint}: ").strip()
    if not raw and default is not None:
        return default
    return int(raw) if raw.isdigit() else default

def _pick_config() -> BuildConfig:
    labels = [f"{c.name:12s} {c.description}" for c in BUILD_CONFIGS]
    return BUILD_CONFIGS[_pick("Select build configuration:", labels)]

def _pick_configs() -> list[BuildConfig]:
    labels  = [f"{c.name:12s} {c.description}" for c in BUILD_CONFIGS]
    indices = _multi_pick("Select configurations (Enter = all):", labels)
    return [BUILD_CONFIGS[i] for i in indices]

def _pick_simulator() -> str:
    labels = [f"{k}  (make {v['run_target']})" for k, v in SIM_TARGETS.items()]
    return list(SIM_TARGETS)[_pick("Select simulator:", labels)]

def _pick_board() -> str:
    boards = list(BOARD_CONFIG)
    return boards[_pick("Select FPGA board:", boards)]

def _pick_tests(pool: list[TestCase]) -> list[TestCase]:
    labels  = [f"{t.app}  —  {t.description}" for t in pool]
    indices = _multi_pick("Select tests to run (Enter = all):", labels)
    return [pool[i] for i in indices]


def _gather_fpga_opts() -> dict:
    opts: dict = {}
    opts["force_program"] = _ask_bool("Force bitstream reprogram? (default: auto-detect — probe first, flash only if needed)")
    opts["skip_program"]  = False
    opts["ext_jtag"]      = _ask_bool("Use external JTAG (Pmod J87) instead of BSCANE2?")
    opts["uart_timeout"]  = _ask_int("UART idle timeout in seconds", default=UART_IDLE_TIMEOUT)
    raw = input("Save logs to directory? (blank = skip): ").strip()
    opts["save_logs"] = Path(raw) if raw else None
    raw = input("Write JSON report to file? (blank = skip): ").strip()
    opts["json_report"] = Path(raw) if raw else None
    return opts

# ── Interactive mode ───────────────────────────────────────────────────────────

def interactive_mode(preset_target: Optional[str] = None) -> None:
    header("\n  HEEPsilon run.py\n")

    if preset_target:
        target = preset_target
    else:
        target_idx = _pick("Select target:", [
            "sim   — compile + simulate, validate UART output",
            "fpga  — compile + program board, capture UART",
            "both  — fpga run, then sim run, then compare outputs",
        ])
        target = ["sim", "fpga", "both"][target_idx]

    if target == "sim":
        _interactive_sim()
    elif target == "fpga":
        _interactive_fpga()
    else:
        _interactive_both()

def _interactive_sim() -> None:
    enabled  = [t for t in TESTS if t.enabled]
    disabled = [t for t in TESTS if not t.enabled]

    sim   = _pick_simulator()
    built = _sim_built(sim)
    print(f"\n  Simulator: {_c(BOLD, sim)}  "
          f"[{_c(GREEN, '✓ built') if built else _c(YELLOW, '✗ not built')}]")
    if not built:
        print(f"  {_c(YELLOW, '  → choose option 2 or 3 to build first')}")

    choice = _pick("Sim — what would you like to do?", [
        "Run tests only    — use existing build",
        "Build + run       — (re)build simulator, then run tests",
        "Full rebuild + run — patches + mcu-gen + build-sim + run tests",
        "CI matrix         — full rebuild across all configs",
        "mcu-gen only",
        "Build simulator only",
        "Apply patches only",
        "List available tests",
        "Back",
    ])

    if choice == 8:  # Back
        interactive_mode()
        return

    if choice == 7:  # List tests
        print()
        header("Enabled tests:")
        for t in enabled:
            print(f"  {_c(GREEN, '✓')} {t.app:28s} {t.description}")
        print()
        header("Disabled tests:")
        for t in disabled:
            print(f"  {_c(RED, '✗')} {t.app:28s} {_c(YELLOW, t.skip_reason)}")
        _interactive_sim()
        return

    if choice == 6:  # Apply patches only
        apply_patches(_ask_bool("Dry run?"))
        return

    if choice == 5:  # Build simulator only
        build_sim(_ask_bool("Dry run?"), simulator=sim)
        return

    if choice == 4:  # mcu-gen only
        dry = _ask_bool("Dry run?")
        cfg = _pick_config()
        mb  = _ask_int("MEMORY_BANKS (blank = per-grid Makefile default)",
                       default=HEEPSILON_DEFAULT_MEMORY_BANKS)
        ensure_python_deps(dry)
        apply_patches(dry)
        mcu_gen(dry, config=cfg, memory_banks=mb)
        return

    # choices 0-3: run tests (with varying build steps)
    run_patches = choice in (2, 3)
    run_gen     = choice in (2, 3)
    run_build   = choice in (1, 2, 3)

    if choice == 0 and not built:
        print(_c(YELLOW, f"\n  ! {sim} simulator not built — tests will likely fail."))
        if not _ask_bool("Continue anyway?"):
            _interactive_sim()
            return

    if choice == 3:  # CI matrix
        configs      = _pick_configs()
        tests_to_run = _pick_tests(enabled)
        dry          = _ask_bool("Dry run?")
        verbose      = _ask_bool("Verbose output?")
        timeout      = _ask_int("Per-test timeout in seconds (blank = none)")
        fail_fast    = _ask_bool("Stop on first failure?")
        if run_patches:
            ensure_python_deps(dry)
            apply_patches(dry)
        results = run_sim_matrix(
            tests_to_run, configs,
            opts={
                "dry_run":      dry,
                "verbose":      verbose,
                "timeout":      timeout,
                "save_logs":    None,
                "repeat":       1,
                "fail_fast":    fail_fast,
                "memory_banks": HEEPSILON_DEFAULT_MEMORY_BANKS,
                "run_gen":      run_gen,
                "run_build":    run_build,
            },
            simulator=sim,
        )
        print_summary(results)
        return

    cfg          = _pick_config()
    tests_to_run = _pick_tests(enabled)
    dry          = _ask_bool("Dry run?")
    verbose      = _ask_bool("Verbose output?")
    timeout      = _ask_int("Per-test timeout in seconds (blank = none)")

    if run_patches:
        ensure_python_deps(dry)
        apply_patches(dry)
    if run_gen:
        mcu_gen(dry, config=cfg, memory_banks=HEEPSILON_DEFAULT_MEMORY_BANKS)
    if run_build:
        build_sim(dry, simulator=sim)

    results = []
    for tc in tests_to_run:
        r = run_sim_test(
            tc,
            verbose   = verbose,
            dry_run   = dry,
            timeout   = timeout,
            save_logs = None,
            conda_env = DEFAULT_CONDA,
            repeat    = 1,
            config    = cfg,
            simulator = sim,
        )
        results.append(r)

    print_summary(results)

def _interactive_fpga() -> None:
    enabled      = [t for t in TESTS if t.enabled]
    board        = _pick_board()
    tests_to_run = _pick_tests(enabled)
    opts         = _gather_fpga_opts()
    sim_verify   = _ask_bool("Also run in sim and compare outputs? (--target both mode)")

    sim = DEFAULT_SIMULATOR
    if sim_verify:
        sim = _pick_simulator()

    results = run_fpga_session(
        tests_to_run, board,
        skip_program  = opts["skip_program"],
        force_program = opts["force_program"],
        ext_jtag      = opts["ext_jtag"],
        uart_timeout  = opts["uart_timeout"] or UART_IDLE_TIMEOUT,
        save_logs     = opts["save_logs"],
        sim_verify    = sim_verify,
        simulator     = sim,
    )
    overall = print_summary(results)
    if opts["json_report"]:
        write_json_report(results, opts["json_report"])
    sys.exit(0 if overall else 1)

def _interactive_both() -> None:
    enabled      = [t for t in TESTS if t.enabled]
    board        = _pick_board()
    tests_to_run = _pick_tests(enabled)
    sim          = _pick_simulator()
    fopts        = _gather_fpga_opts()

    results = run_fpga_session(
        tests_to_run, board,
        skip_program  = fopts["skip_program"],
        force_program = fopts["force_program"],
        ext_jtag      = fopts["ext_jtag"],
        uart_timeout  = fopts["uart_timeout"] or UART_IDLE_TIMEOUT,
        save_logs     = fopts["save_logs"],
        sim_verify    = True,
        simulator     = sim,
    )
    overall = print_summary(results)
    if fopts["json_report"]:
        write_json_report(results, fopts["json_report"])
    sys.exit(0 if overall else 1)

# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    config_names = ", ".join(c.name for c in BUILD_CONFIGS)
    p = argparse.ArgumentParser(
        prog="run.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(f"""\
            HEEPsilon unified run script.

            Available configs: {config_names}

            Target inference (no --target needed):
              --board <non-default>              → fpga
              --simulator <non-default>          → sim
              --board + --simulator              → both (FPGA run then sim compare)

            Examples:
              ./run.py                                              # interactive menu
              ./run.py --tests cgra_fft kernel_test                # sim (verilator)
              ./run.py --board zcu104 --tests cgra_alu_test        # FPGA run, auto-detect bitstream
              ./run.py --board zcu104 --tests hello_world --program  # FPGA, force flash
              ./run.py --board zcu104 --simulator questasim --tests cgra_fft  # FPGA+sim compare
              ./run.py --simulator questasim --tests cgra_fft      # QuestaSim sim (no rebuild)
              ./run.py --simulator verilator --tests cgra_fft --rebuild  # full rebuild then run
              ./run.py --all-configs --rebuild                     # matrix: gen+build per config
              ./run.py --only-build --simulator questasim          # build QuestaSim model only
        """),
    )

    p.add_argument("--target", choices=["sim", "fpga", "both"], default=None,
                   help="Run target: sim (default unless --board is set), fpga, or both")

    sel = p.add_argument_group("test selection")
    sel.add_argument("--tests", nargs="+", metavar="APP",
                     help="Run only these applications (space-separated)")
    sel.add_argument("--list",       action="store_true",
                     help="List available tests and exit")
    sel.add_argument("--xheep-apps", action="store_true",
                     help="Also run auto-discovered X-HEEP apps (disabled by default)")

    build = p.add_argument_group("build control  [sim / both]")
    build.add_argument("--cgra-cfg", metavar="GRID", default=None,
                       choices=available_cgra_cfgs() or None,
                       help="CGRA grid to run on (%(choices)s). Runs mcu-gen "
                            "automatically if the generated tree belongs to a "
                            "different grid. Default: whatever is generated now.")
    build.add_argument("--patch",      action="store_true",
                       help="Apply source patches before running tests")
    build.add_argument("--gen",        action="store_true",
                       help="Run mcu-gen before running tests")
    build.add_argument("--build-sim",  action="store_true",
                       help="Build simulator before running tests")
    build.add_argument("--rebuild",    action="store_true",
                       help="Shorthand for --patch --gen --build-sim")
    build.add_argument("--only-patches", action="store_true",
                       help="Apply patches only, then exit")
    build.add_argument("--only-gen",     action="store_true",
                       help="Run mcu-gen only, then exit")
    build.add_argument("--only-build",   action="store_true",
                       help="Build simulator only, then exit")
    build.add_argument("--config", default=DEFAULT_CONFIG, metavar="NAME",
                       choices=list(BUILD_CONFIGS_BY_NAME),
                       help=f"hjson template for mcu-gen (default: {DEFAULT_CONFIG}; choices: {config_names})")
    build.add_argument("--memory-banks", type=int, default=HEEPSILON_DEFAULT_MEMORY_BANKS,
                       metavar="N",
                       help="RAM bank count passed to mcu-gen; must match the synthesised bitstream "
                            "(default: the per-CGRA_CFG Makefile value — 4x4: 6, others: 12)")
    build.add_argument("--all-configs", action="store_true",
                       help="Run the full sim pipeline for every build config (matrix mode)")
    build.add_argument("--simulator", default=None,
                       choices=list(SIM_TARGETS),
                       help=f"Simulator to use (default: {DEFAULT_SIMULATOR}); "
                            "also implies --target sim when given without --board")

    fpga = p.add_argument_group("FPGA options  [fpga / both]")
    fpga.add_argument("--board", default=DEFAULT_BOARD,
                      choices=list(BOARD_CONFIG),
                      help=f"Target FPGA board (default: {DEFAULT_BOARD})")
    prog = fpga.add_mutually_exclusive_group()
    prog.add_argument("--skip-program", action="store_true",
                      help="Skip probe + programming entirely (trust board as-is)")
    prog.add_argument("--program",      action="store_true",
                      help="Always flash bitstream, skipping auto-probe")
    fpga.add_argument("--ext-jtag",     action="store_true",
                      help="Use external JTAG (Pmod J87) instead of BSCANE2 (ZCU104 only)")
    fpga.add_argument("--uart-timeout", type=int, default=UART_IDLE_TIMEOUT, metavar="SECS",
                      help=f"UART idle timeout in seconds (default: {UART_IDLE_TIMEOUT})")

    run_opts = p.add_argument_group("run options")
    run_opts.add_argument("--verbose",  "-v", action="store_true",
                          help="Print uart0.log / UART capture for every test")
    run_opts.add_argument("--fail-fast",      action="store_true",
                          help="Stop after first test failure")
    run_opts.add_argument("--timeout",        type=int, metavar="SECONDS",
                          help="Kill sim after this many seconds (prevents hangs)")
    run_opts.add_argument("--repeat",         type=int, default=1, metavar="N",
                          help="Run each sim test up to N times; stop on first pass (default: 1)")
    run_opts.add_argument("--save-logs",      metavar="DIR",
                          help="Save uart0.log / UART captures to this directory")
    run_opts.add_argument("--json-report",    metavar="FILE",
                          help="Write a JSON results report to this file")
    run_opts.add_argument("--dry-run", "-n",  action="store_true",
                          help="Print commands without executing them")
    run_opts.add_argument("--conda-env",      default=DEFAULT_CONDA, metavar="ENV",
                          help=f"Conda environment name (default: {DEFAULT_CONDA})")
    return p

# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    board_explicit     = args.board != DEFAULT_BOARD
    simulator_explicit = args.simulator is not None
    target_explicit    = args.target is not None

    sim = args.simulator or DEFAULT_SIMULATOR

    if target_explicit:
        target = args.target
    elif board_explicit and simulator_explicit:
        target = "both"
    elif board_explicit:
        target = "fpga"
    elif simulator_explicit:
        target = "sim"
    else:
        target = args.target or "sim"

    dry         = args.dry_run
    conda       = args.conda_env
    cfg         = BUILD_CONFIGS_BY_NAME[args.config]
    save_logs   = Path(args.save_logs)   if args.save_logs   else None
    json_report = Path(args.json_report) if args.json_report else None

    # ── --list ──────────────────────────────────────────────────────────────────
    if args.list:
        header("\nEnabled tests:")
        for t in TESTS:
            if t.enabled:
                print(f"  {t.app:28s} {t.description}")
        header("\nDisabled tests:")
        for t in TESTS:
            if not t.enabled:
                print(f"  {t.app:28s} SKIP: {t.skip_reason}")
        header("\nAvailable configs:")
        for c in BUILD_CONFIGS:
            marker = " (default)" if c.name == DEFAULT_CONFIG else ""
            print(f"  {c.name:12s} {c.description}{marker}")
        return

    # ── CGRA grid ───────────────────────────────────────────────────────────────
    # Only one grid's generated files can exist in the source tree at a time, so
    # asking for a different grid than the one that is generated means running
    # mcu-gen first. Doing it here saves the caller a separate make invocation
    # and guarantees the two never disagree.
    if args.cgra_cfg and args.cgra_cfg != _stamped_cgra_cfg():
        info(f"Generated tree is for CGRA_CFG={_stamped_cgra_cfg() or '<none>'}, "
             f"switching to {args.cgra_cfg}")
        mcu_gen(dry, conda, cfg, memory_banks=args.memory_banks)
    # A missing simulator otherwise surfaces late as "skipping config" with no
    # hint of what to do. Only relevant when actually simulating.
    if (target in ("sim", "both") and not dry and not args.only_gen
            and not args.only_patches and not _sim_built(sim)
            and not (args.build_sim or args.rebuild or args.only_build)):
        err(f"No {sim} simulator for CGRA_CFG={CGRA_CFG} "
            f"({sim_log_dir(sim)} missing).")
        err(f"  Build it once with:  ./run.py --cgra-cfg {CGRA_CFG} --build-sim")
        sys.exit(1)

    # ── --only-* shortcuts ──────────────────────────────────────────────────────
    if args.only_patches:
        apply_patches(dry)
        return
    if args.only_gen:
        ensure_python_deps(dry)
        apply_patches(dry)
        mcu_gen(dry, conda, cfg, memory_banks=args.memory_banks)
        return
    if args.only_build:
        build_sim(dry, conda, sim)
        return

    has_action = (
        board_explicit or simulator_explicit
        or args.all_configs
        or args.rebuild or args.patch or args.gen or args.build_sim
    )
    if not has_action and not args.tests:
        interactive_mode(preset_target=target if target_explicit else None)
        return

    if not has_action and args.tests:
        # --tests alone: only ask sim/fpga/both, then run non-interactively
        target_idx = _pick("Select target:", [
            "sim   — compile + simulate, validate UART output",
            "fpga  — compile + program board, capture UART",
            "both  — fpga run, then sim run, then compare outputs",
        ])
        target = ["sim", "fpga", "both"][target_idx]
        if target in ("sim", "both") and not simulator_explicit:
            sim = _pick_simulator()

    # ── Resolve test list ───────────────────────────────────────────────────────
    if args.tests:
        unknown = set(args.tests) - set(TESTS_BY_NAME)
        if unknown:
            sys.exit(
                f"Unknown test(s): {', '.join(sorted(unknown))}\n"
                "Run with --list to see available tests."
            )
        tests_to_run = [TESTS_BY_NAME[n] for n in args.tests]
    elif args.xheep_apps:
        tests_to_run = [
            t for t in TESTS
            if t.enabled or t.skip_reason == "not in default CI run"
        ]
    else:
        tests_to_run = [t for t in TESTS if t.enabled]

    # ── FPGA target ─────────────────────────────────────────────────────────────
    if target == "fpga":
        results = run_fpga_session(
            tests_to_run, args.board,
            skip_program  = args.skip_program,
            force_program = args.program,
            ext_jtag      = args.ext_jtag,
            uart_timeout  = args.uart_timeout,
            dry_run       = dry,
            save_logs     = save_logs,
            verbose       = args.verbose,
        )
        overall = print_summary(results)
        if json_report:
            write_json_report(results, json_report)
        sys.exit(0 if overall else 1)

    # ── Both target ─────────────────────────────────────────────────────────────
    if target == "both":
        results = run_fpga_session(
            tests_to_run, args.board,
            skip_program  = args.skip_program,
            force_program = args.program,
            ext_jtag      = args.ext_jtag,
            uart_timeout  = args.uart_timeout,
            dry_run       = dry,
            save_logs     = save_logs,
            verbose       = args.verbose,
            sim_verify    = True,
            simulator     = sim,
        )
        overall = print_summary(results)
        if json_report:
            write_json_report(results, json_report)
        sys.exit(0 if overall else 1)

    rebuild     = args.rebuild
    run_patches = args.patch     or rebuild
    run_gen     = args.gen       or rebuild
    run_build   = args.build_sim or rebuild

    # ── Sim target ──────────────────────────────────────────────────────────────
    if args.all_configs:
        if run_patches:
            ensure_python_deps(dry)
            apply_patches(dry)
        matrix_opts = {
            "dry_run":      dry,
            "verbose":      args.verbose,
            "timeout":      args.timeout,
            "save_logs":    save_logs,
            "repeat":       args.repeat,
            "fail_fast":    args.fail_fast,
            "memory_banks": args.memory_banks,
            "run_gen":      run_gen,
            "run_build":    run_build,
        }
        results = run_sim_matrix(tests_to_run, BUILD_CONFIGS,
                                 opts=matrix_opts, conda_env=conda, simulator=sim)
        overall = print_summary(results)
        if json_report:
            write_json_report(results, json_report)
        sys.exit(0 if overall else 1)

    # Single-config sim pipeline
    if run_patches:
        ensure_python_deps(dry)
        apply_patches(dry)
    if run_gen:
        mcu_gen(dry, conda, cfg, memory_banks=args.memory_banks)
    if run_build:
        build_sim(dry, conda, sim)

    if not dry and not _sim_built(sim):
        sys.exit(
            f"Simulator not built yet ({SIM_TARGETS[sim]['log_dir']}/ not found).\n"
            f"Run:  ./run.py --simulator {sim} --build-sim"
        )

    results = []
    for tc in tests_to_run:
        r = run_sim_test(
            tc,
            verbose   = args.verbose,
            dry_run   = dry,
            timeout   = args.timeout,
            save_logs = save_logs,
            conda_env = conda,
            repeat    = args.repeat,
            config    = cfg,
            simulator = sim,
        )
        results.append(r)
        if args.fail_fast and not r.passed:
            err("Stopping early (--fail-fast)")
            break

    overall = print_summary(results)
    if json_report:
        write_json_report(results, json_report)
    sys.exit(0 if overall else 1)

if __name__ == "__main__":
    main()
