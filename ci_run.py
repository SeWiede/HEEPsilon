#!/usr/bin/env python3
"""
ci_run.py — HEEPsilon CI build and test runner.

Run without arguments for an interactive menu.
Run with --help for all CLI options.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).resolve().parent
SIM_DIR       = SCRIPT_DIR / "build/eslepfl_systems_heepsilon_0/sim-verilator"
MCU_CFG       = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/mcu_cfg.hjson"
HEEPSILON_CORE= SCRIPT_DIR / "heepsilon.core"
DEFAULT_CONDA = "core-v-mini-mcu"

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

# ── Test definitions ───────────────────────────────────────────────────────────

@dataclass
class TestCase:
    app:          str
    pass_pattern: str
    fail_pattern: str = ""
    description:  str = ""
    enabled:      bool = True
    skip_reason:  str = ""

TESTS: list[TestCase] = [
    TestCase("cgra_func_test",        r"finished with 0 errors",
             description="CGRA functionality check"),
    TestCase("cgra_load_store_test",  r"finished with 0 errors",
             description="CGRA load/store check"),
    TestCase("cgra_alu_test",         r"finished with 0 errors",
             description="CGRA ALU operation coverage (21 ops)"),
    TestCase("cgra_leftright_test",   r"finished with 0 errors",
             description="CGRA inter-column data passing via RCL"),
    TestCase("cgra_fullgrid_test",    r"finished with 0 errors",
             description="CGRA full 4×4 grid: 16 distinct functions across all RCs"),
    TestCase("cgra_fft",              r"finished with 0 errors",
             description="CGRA FFT computation"),
    TestCase("kernel_test",           r"E\t0",
             description="Multi-kernel benchmark (conv, reversebits, bitcount, sqrt, "
                         "gsm, strsearch, sha, sha2, sabs)"),
    TestCase("cgra_dbl_search",       r"finished with 0 errors",
             description="CGRA double min/max search"),
    TestCase("mmul_os",               r"Total cgra:",
             enabled=False,
             skip_reason="requires cgra_x_heep.h (not vendored)"),
    TestCase("trans_versasense",      r"END",
             enabled=False,
             skip_reason="requires SYLT-FFT/fft.h (not vendored)"),
    TestCase("transformer",           r"END",
             enabled=False,
             skip_reason="CGRA matmul kernel unsupported matrix sizes; SW fallback too slow"),
]

TESTS_BY_NAME: dict[str, TestCase] = {t.app: t for t in TESTS}

# ── Build configurations ───────────────────────────────────────────────────────

@dataclass
class BuildConfig:
    name:         str
    x_heep_cfg:   str   # relative path under hw/vendor/esl_epfl_x_heep/
    memory_banks: int
    description:  str

BUILD_CONFIGS: list[BuildConfig] = [
    BuildConfig("general",  "configs/general.hjson",  2,
                "2 banks × 32 KB = 64 KB RAM  (minimal)"),
    BuildConfig("cgra",     "configs/cgra.hjson",     6,
                "6 banks × 32 KB = 192 KB RAM (standard CGRA)"),
    BuildConfig("ci",       "configs/ci.hjson",       6,
                "6 banks × 32 KB = 192 KB RAM (CI variant)"),
    BuildConfig("cgra_fat", "configs/cgra_fat.hjson", 12,
                "12 banks × 32 KB = 384 KB RAM (large; default)"),
]

BUILD_CONFIGS_BY_NAME: dict[str, BuildConfig] = {c.name: c for c in BUILD_CONFIGS}
DEFAULT_CONFIG = "cgra_fat"

# ── Environment loading ────────────────────────────────────────────────────────

_env_cache: Optional[dict[str, str]] = None

def load_env() -> dict[str, str]:
    """Source env.sh once, cache and return the resulting environment."""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    info("Sourcing env.sh")
    cmd = f"source {SCRIPT_DIR}/env.sh && env -0"
    result = subprocess.run(
        ["bash", "-c", cmd],
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
    dry_run: bool = False,
    timeout: Optional[int] = None,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run cmd inside the given conda environment with the sourced env.sh
    environment variables.  Returns the CompletedProcess.
    """
    full_cmd = ["conda", "run", "--no-capture-output", "-n", conda_env] + cmd
    if dry_run:
        print(_c(CYAN, f"  [dry-run] {' '.join(full_cmd)}"))
        return subprocess.CompletedProcess(full_cmd, 0, "", "")
    env = load_env()
    kwargs: dict = dict(env=env, cwd=SCRIPT_DIR)
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
    """Apply required source-level modifications if not already present."""
    # 1. stack_size 0x800 → 0x8000
    text = MCU_CFG.read_text()
    if "stack_size: 0x800," in text:
        info(f"Patching {MCU_CFG.name}: stack_size 0x800 → 0x8000")
        if not dry_run:
            MCU_CFG.write_text(text.replace("stack_size: 0x800,", "stack_size: 0x8000,"))
    else:
        info(f"{MCU_CFG.name}: stack_size already correct")

    # 2. -Wno-UNDRIVEN in heepsilon.core
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

# ── Build steps ───────────────────────────────────────────────────────────────

def ensure_python_deps(dry_run: bool = False) -> None:
    info("Checking Python deps (hjson mako jsonref)")
    try:
        import hjson, mako, jsonref  # noqa: F401
        info("All Python deps present")
    except ImportError:
        info("Installing missing deps via pip")
        run_cmd(["pip", "install", "-q", "hjson", "mako", "jsonref"],
                conda_env=DEFAULT_CONDA, dry_run=dry_run)

def mcu_gen(
    dry_run:   bool = False,
    conda_env: str = DEFAULT_CONDA,
    config:    Optional[BuildConfig] = None,
) -> None:
    cfg = config or BUILD_CONFIGS_BY_NAME[DEFAULT_CONFIG]
    info(f"Running mcu-gen  (X_HEEP_CFG={cfg.x_heep_cfg}  MEMORY_BANKS={cfg.memory_banks})  [{cfg.name}]")
    r = run_cmd(
        ["make", "mcu-gen",
         f"X_HEEP_CFG={cfg.x_heep_cfg}",
         f"MEMORY_BANKS={cfg.memory_banks}"],
        conda_env=conda_env, dry_run=dry_run,
    )
    if r.returncode != 0:
        sys.exit("mcu-gen failed — aborting.")

def build_sim(dry_run: bool = False, conda_env: str = DEFAULT_CONDA) -> None:
    info("Building Verilator simulator")
    r = run_cmd(["make", "verilator-sim"], conda_env=conda_env, dry_run=dry_run)
    if r.returncode != 0:
        sys.exit("verilator-sim build failed — aborting.")

# ── Test runner ───────────────────────────────────────────────────────────────

@dataclass
class Result:
    app:    str
    passed: bool
    reason: str = ""
    log:    str = ""
    config: str = ""   # name of BuildConfig used (empty = not applicable)

def run_test(
    tc:         TestCase,
    *,
    verbose:    bool = False,
    dry_run:    bool = False,
    timeout:    Optional[int] = None,
    save_logs:  Optional[Path] = None,
    conda_env:  str = DEFAULT_CONDA,
    repeat:     int = 1,
    config:     Optional[BuildConfig] = None,
) -> Result:
    """
    Build and simulate tc.app, check uart0.log against pass/fail patterns.
    If repeat > 1, re-runs up to that many times; stops on first pass.
    """
    cfg_name = config.name if config else ""
    cfg_tag  = f"[{cfg_name}] " if cfg_name else ""

    for attempt in range(1, repeat + 1):
        if repeat > 1:
            info(f"{cfg_tag}Running {tc.app} (attempt {attempt}/{repeat}) ...")
        else:
            info(f"{cfg_tag}Running {tc.app} ...")

        log_path = SIM_DIR / "uart0.log"
        if log_path.exists():
            log_path.unlink()

        r = run_cmd(
            ["make", "run-verilator", f"PROJECT={tc.app}"],
            conda_env=conda_env, dry_run=dry_run,
            timeout=timeout,
        )

        if dry_run:
            return Result(tc.app, True, "dry-run", config=cfg_name)

        if r.returncode == -1:          # timeout sentinel
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
            parts = [tc.app]
            if cfg_name:
                parts.append(cfg_name)
            if repeat > 1:
                parts.append(f"attempt{attempt}")
            stem = "_".join(parts)
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

    return result  # type: ignore[return-value]  # set in all branches above

# ── Summary + report ──────────────────────────────────────────────────────────

def print_summary(results: list[Result]) -> bool:
    print()
    header("══════════════════════════════════════")
    header("           TEST SUMMARY               ")
    header("══════════════════════════════════════")
    overall = True
    for r in results:
        label = f"[{r.config}] {r.app}" if r.config else r.app
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
        return e
    report = {
        "timestamp": datetime.now().isoformat(),
        "overall": all(r.passed for r in results),
        "results": [_entry(r) for r in results],
    }
    path.write_text(json.dumps(report, indent=2))
    info(f"JSON report written to {path}")

# ── Interactive menu ──────────────────────────────────────────────────────────

def _pick(prompt: str, options: list[str]) -> int:
    """Numbered single-choice menu, returns 0-based index."""
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
    """Numbered multi-choice menu. Empty input = select all."""
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
    raw = input(f"{prompt} {hint} ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")

def _ask_int(prompt: str, default: Optional[int] = None) -> Optional[int]:
    hint = f" (default: {default})" if default is not None else ""
    raw = input(f"{prompt}{hint}: ").strip()
    if not raw and default is not None:
        return default
    return int(raw) if raw.isdigit() else default

def _gather_run_options() -> dict:
    """Ask the user for common run-time options."""
    opts: dict = {}
    opts["verbose"]  = _ask_bool("Verbose output (print uart0.log)?")
    opts["dry_run"]  = _ask_bool("Dry run (print commands, don't execute)?")
    opts["timeout"]  = _ask_int("Per-test timeout in seconds (leave blank = none)")
    opts["repeat"]   = _ask_int("Repeat each test N times (for flaky detection)", default=1)
    raw = input("Save logs to directory? (blank = skip): ").strip()
    opts["save_logs"]= Path(raw) if raw else None
    raw = input("Write JSON report to file? (blank = skip): ").strip()
    opts["json_report"] = Path(raw) if raw else None
    return opts

def _pick_config() -> BuildConfig:
    """Prompt user to choose a single build config."""
    labels = [f"{c.name:12s} {c.description}" for c in BUILD_CONFIGS]
    idx = _pick("Select build configuration:", labels)
    return BUILD_CONFIGS[idx]

def _pick_configs() -> list[BuildConfig]:
    """Prompt user to choose one or more build configs (for matrix mode)."""
    labels = [f"{c.name:12s} {c.description}" for c in BUILD_CONFIGS]
    indices = _multi_pick("Select configurations to test (Enter = all):", labels)
    return [BUILD_CONFIGS[i] for i in indices]

def run_matrix(
    tests:      list[TestCase],
    configs:    list[BuildConfig],
    *,
    opts:       dict,
    conda_env:  str = DEFAULT_CONDA,
) -> list[Result]:
    """
    For each config: mcu-gen → build-sim → run all tests.
    Returns a flat list of Results tagged with config name.
    """
    all_results: list[Result] = []
    for cfg in configs:
        header(f"\n  ── Config: {cfg.name}  ({cfg.description}) ──\n")
        mcu_gen(opts.get("dry_run", False), conda_env, cfg)
        build_sim(opts.get("dry_run", False), conda_env)
        for tc in tests:
            r = run_test(
                tc,
                verbose   = opts.get("verbose", False),
                dry_run   = opts.get("dry_run", False),
                timeout   = opts.get("timeout"),
                save_logs = opts.get("save_logs"),
                conda_env = conda_env,
                repeat    = opts.get("repeat") or 1,
                config    = cfg,
            )
            all_results.append(r)
            if opts.get("fail_fast") and not r.passed:
                err(f"Stopping early (fail-fast) at config={cfg.name}")
                return all_results
    return all_results

def interactive_mode() -> None:
    header("\n  HEEPsilon CI Runner\n")

    enabled  = [t for t in TESTS if t.enabled]
    disabled = [t for t in TESTS if not t.enabled]

    MENU = [
        "Full CI  (patches + mcu-gen + build + all enabled tests)",
        "Full CI — matrix across multiple configs",
        "Tests only  (skip mcu-gen and build)",
        "Selected tests",
        "mcu-gen only",
        "Build Verilator simulator only",
        "Apply source patches only",
        "List available tests",
        "Quit",
    ]

    choice = _pick("What would you like to do?", MENU)

    if choice == 8:
        sys.exit(0)

    if choice == 7:
        print()
        header("Enabled tests:")
        for t in enabled:
            print(f"  {_c(GREEN, '✓')} {t.app:28s} {t.description}")
        print()
        header("Disabled tests:")
        for t in disabled:
            print(f"  {_c(RED, '✗')} {t.app:28s} {_c(YELLOW, t.skip_reason)}")
        interactive_mode()
        return

    # Build-only shortcuts (no run options needed)
    if choice == 4:   # mcu-gen only
        dry_run = _ask_bool("Dry run?")
        cfg = _pick_config()
        ensure_python_deps(dry_run)
        apply_patches(dry_run)
        mcu_gen(dry_run, config=cfg)
        return
    if choice == 5:   # build sim only
        dry_run = _ask_bool("Dry run?")
        build_sim(dry_run)
        return
    if choice == 6:   # patches only
        dry_run = _ask_bool("Dry run?")
        apply_patches(dry_run)
        return

    # Matrix mode
    if choice == 1:
        configs = _pick_configs()
        tests_to_run = enabled
        opts = _gather_run_options()
        opts["fail_fast"] = _ask_bool("Stop matrix on first failure?")
        ensure_python_deps(opts["dry_run"])
        apply_patches(opts["dry_run"])
        results = run_matrix(tests_to_run, configs, opts=opts)
        overall = print_summary(results)
        if opts["json_report"]:
            write_json_report(results, opts["json_report"])
        sys.exit(0 if overall else 1)

    # Choices that involve running tests with a single config
    cfg = _pick_config()
    opts = _gather_run_options()

    tests_to_run = enabled
    if choice == 0:   # Full CI
        ensure_python_deps(opts["dry_run"])
        apply_patches(opts["dry_run"])
        mcu_gen(opts["dry_run"], config=cfg)
        build_sim(opts["dry_run"])
    elif choice == 2:  # Tests only
        pass
    elif choice == 3:  # Selected
        labels  = [f"{t.app}  —  {t.description}" for t in enabled]
        indices = _multi_pick("Select tests to run:", labels)
        tests_to_run = [enabled[i] for i in indices]

    results: list[Result] = []
    for tc in tests_to_run:
        r = run_test(
            tc,
            verbose   = opts["verbose"],
            dry_run   = opts["dry_run"],
            timeout   = opts["timeout"],
            save_logs = opts["save_logs"],
            repeat    = opts["repeat"] or 1,
            config    = cfg,
        )
        results.append(r)

    overall = print_summary(results)
    if opts["json_report"]:
        write_json_report(results, opts["json_report"])
    sys.exit(0 if overall else 1)

# ── CLI argument mode ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    config_names = ", ".join(c.name for c in BUILD_CONFIGS)
    p = argparse.ArgumentParser(
        prog="ci_run.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(f"""\
            HEEPsilon CI build and test runner.
            Run without arguments for an interactive menu.

            Available configs: {config_names}

            Examples:
              ./ci_run.py                                  # interactive menu
              ./ci_run.py --skip-gen --skip-build          # run all tests, no rebuild
              ./ci_run.py --tests cgra_fft kernel_test     # run two specific tests
              ./ci_run.py --only-build                     # just (re)build the simulator
              ./ci_run.py --list                           # show available tests
              ./ci_run.py --config cgra                    # use cgra memory config
              ./ci_run.py --all-configs --skip-build       # test across all configs (no rebuild)
        """),
    )

    sel = p.add_argument_group("test selection")
    sel.add_argument("--tests", nargs="+", metavar="APP",
                     help="Run only these applications (space-separated)")
    sel.add_argument("--list", action="store_true",
                     help="List available tests and exit")

    build = p.add_argument_group("build control")
    build.add_argument("--skip-patches", action="store_true",
                       help="Skip source-file patch step")
    build.add_argument("--skip-gen",     action="store_true",
                       help="Skip mcu-gen step")
    build.add_argument("--skip-build",   action="store_true",
                       help="Skip Verilator build step")
    build.add_argument("--only-patches", action="store_true",
                       help="Apply patches only, then exit")
    build.add_argument("--only-gen",     action="store_true",
                       help="Run mcu-gen only, then exit")
    build.add_argument("--only-build",   action="store_true",
                       help="Build simulator only, then exit")
    build.add_argument("--config",       default=DEFAULT_CONFIG,
                       metavar="NAME",
                       choices=list(BUILD_CONFIGS_BY_NAME),
                       help=f"Memory config to use for mcu-gen (default: {DEFAULT_CONFIG}; "
                            f"choices: {config_names})")
    build.add_argument("--all-configs",  action="store_true",
                       help="Run the full pipeline for every build config (matrix mode). "
                            "Implies --config is ignored; builds sim once per config.")

    run_opts = p.add_argument_group("run options")
    run_opts.add_argument("--verbose", "-v",   action="store_true",
                          help="Print uart0.log for every test")
    run_opts.add_argument("--fail-fast",        action="store_true",
                          help="Stop after first test failure")
    run_opts.add_argument("--timeout",          type=int, metavar="SECONDS",
                          help="Kill simulation after this many seconds (prevents hangs)")
    run_opts.add_argument("--repeat",           type=int, default=1, metavar="N",
                          help="Run each test up to N times; stop on first pass (default: 1)")
    run_opts.add_argument("--save-logs",        metavar="DIR",
                          help="Copy uart0.log for each test into this directory")
    run_opts.add_argument("--json-report",      metavar="FILE",
                          help="Write a JSON results report to this file")
    run_opts.add_argument("--dry-run", "-n",    action="store_true",
                          help="Print commands without executing them")
    run_opts.add_argument("--conda-env",        default=DEFAULT_CONDA, metavar="ENV",
                          help=f"Conda environment name (default: {DEFAULT_CONDA})")
    return p

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    # No actionable flags → interactive
    has_action = any([
        args.tests, args.list,
        args.skip_patches, args.skip_gen, args.skip_build,
        args.only_patches, args.only_gen, args.only_build,
        args.all_configs,
        # --config alone (non-default) signals intent to run
        args.config != DEFAULT_CONFIG,
    ])
    if not has_action:
        interactive_mode()
        return

    conda       = args.conda_env
    dry         = args.dry_run
    cfg         = BUILD_CONFIGS_BY_NAME[args.config]
    save_logs   = Path(args.save_logs)   if args.save_logs   else None
    json_report = Path(args.json_report) if args.json_report else None

    # --list
    if args.list:
        header("\nEnabled tests:")
        for t in TESTS:
            if t.enabled:
                print(f"  {t.app:28s} {t.description}")
        header("\nDisabled tests:")
        for t in TESTS:
            if not t.enabled:
                print(f"  {t.app:28s} SKIP: {t.skip_reason}")
        header(f"\nAvailable configs:")
        for c in BUILD_CONFIGS:
            marker = " (default)" if c.name == DEFAULT_CONFIG else ""
            print(f"  {c.name:12s} {c.description}{marker}")
        return

    # --only-* shortcuts
    if args.only_patches:
        apply_patches(dry)
        return
    if args.only_gen:
        ensure_python_deps(dry)
        apply_patches(dry)
        mcu_gen(dry, conda, cfg)
        return
    if args.only_build:
        build_sim(dry, conda)
        return

    # Resolve test list
    if args.tests:
        unknown = set(args.tests) - set(TESTS_BY_NAME)
        if unknown:
            sys.exit(
                f"Unknown test(s): {', '.join(sorted(unknown))}\n"
                "Run with --list to see available tests."
            )
        tests_to_run = [TESTS_BY_NAME[n] for n in args.tests]
    else:
        tests_to_run = [t for t in TESTS if t.enabled]

    # --all-configs matrix mode
    if args.all_configs:
        if not args.skip_patches:
            ensure_python_deps(dry)
            apply_patches(dry)
        matrix_opts = {
            "dry_run":   dry,
            "verbose":   args.verbose,
            "timeout":   args.timeout,
            "save_logs": save_logs,
            "repeat":    args.repeat,
            "fail_fast": args.fail_fast,
        }
        # In matrix mode, --skip-gen/--skip-build apply per-config iteration
        configs_to_run = BUILD_CONFIGS
        if args.skip_gen and args.skip_build:
            # No rebuild at all — just re-run tests for each config label
            results: list[Result] = []
            for c in configs_to_run:
                for tc in tests_to_run:
                    r = run_test(tc, config=c, conda_env=conda, **{
                        k: v for k, v in matrix_opts.items() if k != "fail_fast"
                    })
                    results.append(r)
                    if args.fail_fast and not r.passed:
                        err("Stopping early (--fail-fast)")
                        overall = print_summary(results)
                        if json_report:
                            write_json_report(results, json_report)
                        sys.exit(0 if overall else 1)
        else:
            results = run_matrix(tests_to_run, configs_to_run, opts=matrix_opts, conda_env=conda)
        overall = print_summary(results)
        if json_report:
            write_json_report(results, json_report)
        sys.exit(0 if overall else 1)

    # Single-config normal pipeline
    if not args.skip_patches:
        ensure_python_deps(dry)
        apply_patches(dry)
    if not args.skip_gen:
        mcu_gen(dry, conda, cfg)
    if not args.skip_build:
        build_sim(dry, conda)

    results = []
    for tc in tests_to_run:
        r = run_test(
            tc,
            verbose   = args.verbose,
            dry_run   = dry,
            timeout   = args.timeout,
            save_logs = save_logs,
            conda_env = conda,
            repeat    = args.repeat,
            config    = cfg,
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
