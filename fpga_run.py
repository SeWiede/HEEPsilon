#!/usr/bin/env python3
"""
fpga_run.py — PYNQ-Z1 FPGA workflow automation for HEEPsilon.
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).resolve().parent
BITSTREAM    = SCRIPT_DIR / "build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit"
BUILDVIVADO  = SCRIPT_DIR / "buildvivado.log"
OPENOCD_CFG  = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z1-bscan.cfg"
SW_BUILD     = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/sw/build"
APPS_DIR     = SCRIPT_DIR / "sw/applications"
CONDA_ENV    = "core-v-mini-mcu"
PYNQ_Z1_VID_PID = "0403:6010"

# Per-app sentinel strings; apps not listed fall back to UART idle timeout.
# All apps that gate output on #ifdef DEBUG will print because build_app
# always sets CDEFS=DEBUG.
# Self-written apps all print "### DONE ###" as their last line — they use the
# default sentinel and need no entry here.
# Upstream apps have no standardised terminal line, so they get individual entries.
APP_SENTINELS: dict[str, str] = {
    "hello_world":          "hello world!",
    "cgra_load_store_test": "functionality check finished with",
    "cgra_fft":             "FFT computation finished with",
    "cgra_check_conf":      "CGRA configuration check finished with",
    "mmul_os":              "Total cgra:",
    "transformer":          "END",
    # kernel_test prints "E\t<n>" as its last line
    "kernel_test":          "E\t",
    # cgra_func_test / cgra_dbl_search / trans_versasense have no clean terminal
    # line — they fall through to the idle timeout.
}

UART_IDLE_TIMEOUT = 30  # seconds after last received character

# ── Colour helpers ─────────────────────────────────────────────────────────────

RESET  = "\033[0m"
RED    = "\033[0;31m"
GREEN  = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"

def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text

def info(msg: str)    -> None: print(_c(YELLOW, "[INFO]"), msg)
def ok(msg: str)      -> None: print(_c(GREEN,  "[OK]"),   msg)
def err(msg: str)     -> None: print(_c(RED,    "[ERR]"),  msg, file=sys.stderr)
def header(msg: str)  -> None: print(_c(BOLD,   f"\n── {msg} ──"))

# ── Environment ────────────────────────────────────────────────────────────────

def make_env() -> dict[str, str]:
    env = os.environ.copy()
    env["XILINX_VIVADO"] = os.path.expanduser("~/tools/Xilinx/Vivado/2022.2")
    env["PATH"] = ":".join([
        os.path.expanduser("~/tools/Xilinx/Vivado/2022.2/bin"),
        os.path.expanduser("~/tools/openocd/bin"),
        os.path.expanduser("~/tools/riscv/2022.01.17/bin"),
        os.path.expanduser("~/tools/verilator/4.210/bin"),
        env["PATH"],
    ])
    env["RISCV"]       = os.path.expanduser("~/tools/riscv/2022.01.17")
    env["RISCV_XHEEP"] = os.path.expanduser("~/tools/riscv/2022.01.17")
    return env

ENV = make_env()

def conda_cmd(cmd: list[str]) -> list[str]:
    return ["conda", "run", "--no-capture-output", "-n", CONDA_ENV] + cmd

# ── Subprocess tracking (for cleanup) ─────────────────────────────────────────

_subprocesses: list[subprocess.Popen] = []

def _track(proc: subprocess.Popen) -> subprocess.Popen:
    _subprocesses.append(proc)
    return proc

def _cleanup() -> None:
    for proc in _subprocesses:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

# ── Step 1: Board detection ────────────────────────────────────────────────────

def check_board() -> None:
    header("Board detection")
    result = subprocess.run(["lsusb"], capture_output=True, text=True)
    if PYNQ_Z1_VID_PID not in result.stdout:
        sys.exit(
            f"PYNQ-Z1 not found (USB VID:PID {PYNQ_Z1_VID_PID} / FT2232H not detected).\n"
            "Check that the board is powered and connected via USB-JTAG."
        )
    ok(f"PYNQ-Z1 detected (VID:PID {PYNQ_Z1_VID_PID})")

# ── Step 2: Bitstream check ────────────────────────────────────────────────────

def _bitstream_is_fresh() -> bool:
    if not BITSTREAM.exists():
        return False
    if not BUILDVIVADO.exists():
        return False
    return "BSCANE2" in BUILDVIVADO.read_text()

def check_bitstream() -> None:
    header("Bitstream check")
    if _bitstream_is_fresh():
        ok("Bitstream ready")
        return

    if not BITSTREAM.exists():
        reason = f"{BITSTREAM.name} not found"
    else:
        reason = "buildvivado.log missing --flag=use_bscane_xilinx (stale build)"

    info(f"Bitstream not ready: {reason}")
    answer = input("Rebuild bitstream now? This takes a long time. [y/N] ").strip().lower()
    if not answer.startswith("y"):
        sys.exit("Bitstream not available — aborting.")

    header("Building FPGA bitstream")
    cmd = conda_cmd([
        "make", "vivado-fpga",
        "FPGA_BOARD=pynq-z1",
        "FUSESOC_FLAGS=--flag=use_bscane_xilinx",
    ])
    result = subprocess.run(cmd, env=ENV, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit("vivado-fpga build failed.")
    if not _bitstream_is_fresh():
        sys.exit("Build completed but bitstream still not valid — check buildvivado.log.")
    ok("Bitstream built successfully")

# ── Step 3: Flash bitstream ────────────────────────────────────────────────────

def program_bitstream() -> None:
    header("Programming bitstream")
    cmd = [
        "vivado", "-nolog", "-nojournal", "-mode", "batch",
        "-source", str(SCRIPT_DIR / "program_fpga.tcl"),
    ]
    result = subprocess.run(cmd, env=ENV, cwd=SCRIPT_DIR, capture_output=True, text=True)
    combined = result.stdout + result.stderr
    if "End of startup status: HIGH" not in combined:
        print(combined)
        sys.exit("Programming failed: 'End of startup status: HIGH' not seen in Vivado output.")
    ok("Bitstream programmed — startup status HIGH")

# ── Step 4: Interactive app menu ───────────────────────────────────────────────

def pick_app(app_arg: str | None) -> str:
    apps = sorted(p.name for p in APPS_DIR.iterdir() if p.is_dir())
    if not apps:
        sys.exit(f"No applications found in {APPS_DIR}")

    if app_arg is not None:
        if app_arg not in apps:
            sys.exit(f"Unknown app '{app_arg}'. Available: {', '.join(apps)}")
        info(f"Using app: {app_arg}")
        return app_arg

    header("Application menu")
    for i, name in enumerate(apps, 1):
        print(f"  {_c(CYAN, str(i)):>6}. {name}")
    while True:
        raw = input("\nSelect app number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(apps):
            chosen = apps[int(raw) - 1]
            info(f"Selected: {chosen}")
            return chosen
        print(_c(RED, "Invalid choice — enter a number from the list."))

# ── Step 5: Build app ──────────────────────────────────────────────────────────

def build_app(app: str) -> None:
    header(f"Building app: {app}")
    cmd = conda_cmd(["make", "app", f"PROJECT={app}", "LINKER=on_chip", "TARGET=pynq-z1"])
    # CDEFS=DEBUG ensures apps gated on #ifdef DEBUG produce UART output
    env = {**ENV, "CDEFS": "DEBUG"}
    result = subprocess.run(cmd, env=env, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit(f"make app failed for {app}.")
    ok(f"{app} compiled → {SW_BUILD}/main.elf")

# ── Step 6: Start OpenOCD ──────────────────────────────────────────────────────

def _drain_stdout(proc: subprocess.Popen, prefix: str) -> None:
    """Daemon thread: drain proc.stdout to console so the pipe never fills up."""
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            print(f"  [{prefix}] {line}", end="")
    except Exception:
        pass


def start_openocd() -> subprocess.Popen:
    header("Starting OpenOCD")
    cmd = ["openocd", "-f", str(OPENOCD_CFG)]
    proc = _track(subprocess.Popen(
        cmd, env=ENV, cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    ))
    info(f"OpenOCD PID {proc.pid} — waiting for 'Ready for Remote Connections' ...")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if proc.poll() is not None:
                sys.exit(f"OpenOCD exited unexpectedly (rc={proc.returncode}).")
            continue
        print(f"  [openocd] {line}", end="")
        if "Ready for Remote Connections" in line:
            ok("OpenOCD ready")
            # Keep draining stdout so the pipe never blocks OpenOCD
            t = threading.Thread(target=_drain_stdout, args=(proc, "openocd"), daemon=True)
            t.start()
            return proc

    sys.exit("OpenOCD did not become ready within 60 s.")

# ── Step 7: Find UART port ─────────────────────────────────────────────────────

def find_uart() -> str:
    header("Locating UART port")
    by_id = Path("/dev/serial/by-id")
    if by_id.exists():
        for entry in by_id.iterdir():
            name = entry.name
            if "CP2102" in name or "Silicon_Labs" in name:
                port = str(entry.resolve())
                ok(f"CP2102 UART found: {port}  (via {entry})")
                return port
    info("CP2102 not found in /dev/serial/by-id.")
    port = input("Enter UART port manually (e.g. /dev/ttyUSB0): ").strip()
    if not port:
        sys.exit("No UART port specified.")
    return port

# ── Step 8: Open UART ──────────────────────────────────────────────────────────

def open_uart(port: str, baud: int = 9600):
    """Return an open serial.Serial instance."""
    try:
        import serial  # type: ignore
    except ImportError:
        sys.exit("pyserial is not installed. Run: pip install pyserial")
    ser = serial.Serial(port, baud, timeout=0.1)
    ok(f"UART opened: {port} @ {baud} baud")
    return ser

# ── Step 9: Load and run via GDB ───────────────────────────────────────────────

def start_gdb(reset_first: bool = False) -> subprocess.Popen:
    header("Launching GDB")
    elf = SW_BUILD / "main.elf"
    if not elf.exists():
        sys.exit(f"main.elf not found at {elf}")

    cmd = ["gdb-multiarch", str(elf)]
    proc = _track(subprocess.Popen(
        cmd, env=ENV, cwd=SCRIPT_DIR,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    ))

    def _send(line: str) -> None:
        proc.stdin.write(line + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()             # type: ignore[union-attr]

    info(f"GDB PID {proc.pid}")
    time.sleep(0.5)
    _send("set remotetimeout 2000")
    _send("target remote localhost:3333")
    if reset_first:
        # Halt and reset CPU via debug module — used when bitstream flash is skipped
        _send("monitor reset halt")
    _send("load")
    _send("continue")
    ok("GDB: commands sent (set remotetimeout / target remote / load / continue)")
    return proc

# ── Step 10: Capture UART output ───────────────────────────────────────────────

def capture_uart(ser, app: str, idle_timeout: int = UART_IDLE_TIMEOUT) -> str:
    """
    Read UART in a background thread, collect lines until sentinel or idle timeout.
    Returns the full captured output as a string.
    """
    sentinel = APP_SENTINELS.get(app, "### DONE ###")
    info(f"Capturing UART output (sentinel: {sentinel!r}, idle timeout: {idle_timeout}s)")

    buf: list[str] = []
    done_event = threading.Event()
    char_queue: queue.Queue[str] = queue.Queue()

    def _reader():
        try:
            while not done_event.is_set():
                data = ser.read(256)
                if data:
                    char_queue.put(data.decode("utf-8", errors="replace"))
        except Exception:
            pass

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    line_buf = ""
    last_char_time = time.monotonic()

    try:
        while True:
            try:
                chunk = char_queue.get(timeout=0.1)
                last_char_time = time.monotonic()
                sys.stdout.write(chunk)
                sys.stdout.flush()
                buf.append(chunk)
                line_buf += chunk
                if sentinel in line_buf:
                    break
            except queue.Empty:
                if time.monotonic() - last_char_time > idle_timeout:
                    info(f"Idle timeout ({idle_timeout}s) — stopping capture")
                    break
    finally:
        done_event.set()

    return "".join(buf)

# ── Step 11: Cleanup ───────────────────────────────────────────────────────────

def cleanup(
    openocd: subprocess.Popen | None,
    gdb: subprocess.Popen | None,
    ser,
) -> None:
    header("Cleanup")
    for name, proc in [("GDB", gdb), ("OpenOCD", openocd)]:
        if proc is None:
            continue
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            info(f"{name} terminated")
    if ser is not None:
        try:
            ser.close()
            info("Serial port closed")
        except Exception:
            pass

# ── Step 12: Verilator verification ───────────────────────────────────────────

CYCLE_COUNT_PATTERNS = (
    "active cycles",
    "stall cycles",
    "CGRA kernel executed",
    "spent_cy",
    "cycles:",
)

def _normalise(text: str) -> list[str]:
    """Strip \r, drop blank lines, drop cycle-count lines (differ sim vs HW)."""
    lines = []
    for line in text.replace("\r", "").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if any(p in line for p in CYCLE_COUNT_PATTERNS):
            continue
        lines.append(line)
    return lines

_SIM_TARGETS = {
    "verilator":  ("run-verilator",  "sim-verilator"),
    "questasim":  ("run-questasim",  "sim-modelsim"),
}

def sim_output(app: str, simulator: str = "verilator") -> str:
    make_target, log_dir = _SIM_TARGETS[simulator]
    header(f"{simulator} simulation: {app}")
    # Match FPGA build: pass CDEFS=DEBUG so PRINTF()-gated output is visible
    env = {**ENV, "CDEFS": "DEBUG"}
    cmd = conda_cmd(["make", make_target, f"PROJECT={app}"])
    result = subprocess.run(cmd, env=env, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        err(f"{simulator} simulation failed — skipping comparison.")
        return ""
    uart_log = SCRIPT_DIR / f"build/eslepfl_systems_heepsilon_0/{log_dir}/uart0.log"
    if uart_log.exists():
        return uart_log.read_text(errors="replace")
    alt = SCRIPT_DIR / "uart0.log"
    if alt.exists():
        return alt.read_text(errors="replace")
    err("uart0.log not found after simulation.")
    return ""

def compare_outputs(fpga_out: str, sim_out: str) -> None:
    header("FPGA vs Verilator comparison")
    info("Cycle-count lines excluded (expected to differ between sim and HW)")
    fpga_lines = _normalise(fpga_out)
    sim_lines  = _normalise(sim_out)
    max_len = max(len(fpga_lines), len(sim_lines), 1)
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

# ── Main ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fpga_run.py",
        description="PYNQ-Z1 FPGA workflow automation for HEEPsilon.",
    )
    p.add_argument("app",      metavar="NAME", nargs="?",
                   help="Application to run (skip interactive menu)")
    p.add_argument("--app",   metavar="NAME", dest="app_flag",
                   help=argparse.SUPPRESS)
    p.add_argument("--verify", action="store_true",
                   help="Also run in simulator and compare UART output line-by-line")
    p.add_argument("--sim", choices=["verilator", "questasim"], default="verilator",
                   help="Simulator to use with --verify (default: verilator)")
    flash_grp = p.add_mutually_exclusive_group()
    flash_grp.add_argument("--skip-program", action="store_true",
                   help="Skip bitstream programming (board already configured); "
                        "uses 'monitor reset halt' via GDB to get a clean CPU state")
    flash_grp.add_argument("--program", action="store_true",
                   help="Always program bitstream without prompting")
    p.add_argument("--uart-timeout", metavar="SECS", type=int, default=UART_IDLE_TIMEOUT,
                   help=f"UART idle timeout in seconds (default: {UART_IDLE_TIMEOUT})")
    return p

def main() -> None:
    os.chdir(SCRIPT_DIR)

    args = build_parser().parse_args()
    args.app = args.app or args.app_flag

    openocd_proc: subprocess.Popen | None = None
    gdb_proc:     subprocess.Popen | None = None
    ser = None

    try:
        # 1. Board detection
        check_board()

        # 2. Bitstream check
        check_bitstream()

        # 3. Program (skip if --skip-program, always if --program, else ask)
        if args.skip_program:
            info("Skipping bitstream programming (--skip-program)")
        elif args.program or input("Program bitstream to board? [Y/n] ").strip().lower() not in ("n", "no"):
            program_bitstream()
        else:
            info("Skipping bitstream programming")

        # 4. App selection
        app = pick_app(args.app)

        # 5. Build app
        build_app(app)

        # 6. OpenOCD
        openocd_proc = start_openocd()

        # 7. UART
        uart_port = find_uart()

        # 8. Open UART
        header("Opening UART")
        ser = open_uart(uart_port)

        # 9. GDB
        gdb_proc = start_gdb(reset_first=args.skip_program)

        # 10. Capture
        header("Running application")
        fpga_output = capture_uart(ser, app, idle_timeout=args.uart_timeout)

        print()
        header("FPGA UART output")
        print(fpga_output)

        # 11. Cleanup
        cleanup(openocd_proc, gdb_proc, ser)
        openocd_proc = gdb_proc = ser = None

        # 12. Optional sim verify
        if args.verify:
            sim_out = sim_output(app, simulator=args.sim)
            if sim_out:
                compare_outputs(fpga_output, sim_out)

        ok("Done.")

    except KeyboardInterrupt:
        print()
        info("Interrupted by user — cleaning up.")
        cleanup(openocd_proc, gdb_proc, ser)
        _cleanup()
        sys.exit(1)
    except SystemExit:
        _cleanup()
        raise
    except Exception as exc:
        err(f"Unexpected error: {exc}")
        _cleanup()
        sys.exit(1)

if __name__ == "__main__":
    main()
