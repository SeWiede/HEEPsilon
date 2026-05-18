#!/usr/bin/env python3
"""
fpga_run.py — FPGA workflow automation for HEEPsilon (PYNQ-Z1, PYNQ-Z2, ZCU104).
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
BUILDVIVADO  = SCRIPT_DIR / "buildvivado.log"
SW_BUILD     = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/sw/build"
APPS_DIR     = SCRIPT_DIR / "sw/applications"
CONDA_ENV    = "core-v-mini-mcu"

# Board configurations
BOARD_CONFIG = {
    "pynq-z1": {
        "vid_pid": "0403:6010",
        "bitstream": SCRIPT_DIR / "build/eslepfl_systems_heepsilon_0/pynq-z1-vivado/eslepfl_systems_heepsilon_0.bit",
        "openocd_cfg": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z2-bscan.cfg",
        "program_tcl": SCRIPT_DIR / "program_fpga.tcl",
        "vivado_part": "xc7z020",
        "uart_search": ["CP2102", "Silicon_Labs"],
        "baud": 9600,
        "linker": "on_chip",
        "use_openocd": True,
    },
    "pynq-z2": {
        "vid_pid": "0403:6010",
        "bitstream": SCRIPT_DIR / "build/eslepfl_systems_heepsilon_0/pynq-z2-vivado/eslepfl_systems_heepsilon_0.bit",
        "openocd_cfg": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-pynq-z2-bscan.cfg",
        "program_tcl": SCRIPT_DIR / "program_fpga.tcl",
        "vivado_part": "xc7z020",
        "uart_search": ["CP2102", "Silicon_Labs"],
        "baud": 9600,
        "linker": "on_chip",
        "use_openocd": True,
    },
    "zcu104": {
        "vid_pid": "0403:6011",
        "bitstream": SCRIPT_DIR / "build/eslepfl_systems_heepsilon_0/zcu104-vivado/eslepfl_systems_heepsilon_0.bit",
        # BSCANE2 path (--gdb): FT4232H channel A, requires use_bscane_xilinx bitstream
        "openocd_cfg": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-zcu104-bscan.cfg",
        # External JTAG path (--gdb --ext-jtag): Digilent HS2 on Pmod J87, no BSCANE2 needed
        "openocd_cfg_ext": SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/tb/core-v-mini-mcu-zcu104-ext-jtag.cfg",
        "program_tcl": SCRIPT_DIR / "program_fpga_zcu104.tcl",
        "vivado_part": "xczu7ev",
        # PL UART goes through FT4232H channel D (interface 3) on the single
        # "USB JTAG UART" cable.  uart_tx_o = A20, uart_rx_i = C19 (LVCMOS18).
        # FT4232H channel map: A=JTAG, B=if01/PS-UART0, C=if02/PS-UART1, D=if03/PL-UART.
        "uart_search": [["Xilinx_JTAG+3Serial", "if03"]],
        "baud": 9600,
        "linker": "on_chip",  # ELF loaded via GDB over OpenOCD/BSCANE2
        "use_openocd": True,
    },
}

# Per-app sentinel strings
APP_SENTINELS: dict[str, str] = {
    "hello_world":          "hello world!",
    "cgra_load_store_test": "functionality check finished with",
    "cgra_fft":             "FFT computation finished with",
    "cgra_check_conf":      "CGRA configuration check finished with",
    "mmul_os":              "Total cgra:",
    "transformer":          "END",
    "kernel_test":          "E\t",
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
        os.path.expanduser("~/tools/riscv/corev-2024.05.30/bin"),
        os.path.expanduser("~/tools/verilator/5.040/bin"),
        env["PATH"],
    ])
    env["RISCV"]       = os.path.expanduser("~/tools/riscv/corev-2024.05.30")
    env["RISCV_XHEEP"] = os.path.expanduser("~/tools/riscv/corev-2024.05.30")
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

def check_board(board: str) -> None:
    header("Board detection")
    config = BOARD_CONFIG.get(board)
    if not config:
        sys.exit(f"Unknown board: {board}. Available: {', '.join(BOARD_CONFIG.keys())}")
    
    result = subprocess.run(["lsusb"], capture_output=True, text=True)
    if config["vid_pid"] not in result.stdout:
        sys.exit(
            f"{board} not found (USB VID:PID {config['vid_pid']} not detected).\n"
            "Check that the board is powered and connected via USB-JTAG."
        )
    ok(f"{board} detected (VID:PID {config['vid_pid']})")

# ── Step 2: Bitstream check ────────────────────────────────────────────────────

def _bitstream_is_fresh(board: str, config: dict) -> bool:
    bitstream = config["bitstream"]
    if not bitstream.exists():
        return False
    # Check bitstream exists and is recent
    return bitstream.stat().st_size > 100000  # At least 100KB

def check_bitstream(board: str, gdb: bool = False) -> None:
    header("Bitstream check")
    config = BOARD_CONFIG[board]

    if _bitstream_is_fresh(board, config):
        ok(f"Bitstream ready: {config['bitstream'].name}")
        return

    info(f"Bitstream not found: {config['bitstream']}")
    answer = input("Rebuild bitstream now? This takes a long time. [y/N] ").strip().lower()
    if not answer.startswith("y"):
        sys.exit("Bitstream not available — aborting.")

    header("Building FPGA bitstream")
    fusesoc_flags = ""
    if board in ("pynq-z1", "pynq-z2"):
        fusesoc_flags = "--flag=use_bscane_xilinx"
    elif board == "zcu104" and gdb:
        # --gdb via BSCANE2 requires use_bscane_xilinx; ext-jtag doesn't but it doesn't hurt
        fusesoc_flags = "--flag=use_bscane_xilinx"
    
    cmd = conda_cmd([
        "make", "vivado-fpga",
        f"FPGA_BOARD={board}",
    ] + ([f"FUSESOC_FLAGS={fusesoc_flags}"] if fusesoc_flags else []))
    result = subprocess.run(cmd, env=ENV, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit("vivado-fpga build failed.")
    if not _bitstream_is_fresh(board, config):
        sys.exit("Build completed but bitstream still not valid — check build log.")
    ok("Bitstream built successfully")

# ── Step 3: Flash bitstream ────────────────────────────────────────────────────

def program_bitstream(board: str) -> None:
    header("Programming bitstream")
    config = BOARD_CONFIG[board]
    cmd = [
        "vivado", "-nolog", "-nojournal", "-mode", "batch",
        "-source", str(config["program_tcl"]),
    ]
    result = subprocess.run(cmd, env=ENV, cwd=SCRIPT_DIR, capture_output=True, text=True)
    combined = result.stdout + result.stderr
    if "End of startup status: HIGH" not in combined:
        print(combined)
        sys.exit("Programming failed — 'End of startup status: HIGH' not found. Check Vivado output.")
    ok(f"Bitstream programmed for {board}")

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

def build_app(app: str, board: str, force_on_chip: bool = False) -> None:
    header(f"Building app: {app}")
    linker = "on_chip" if force_on_chip else BOARD_CONFIG[board].get("linker", "on_chip")
    cmd = conda_cmd(["make", "app", f"PROJECT={app}", f"LINKER={linker}", f"TARGET={board}"])
    env = {**ENV, "CDEFS": "DEBUG"}
    result = subprocess.run(cmd, env=env, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit(f"make app failed for {app}.")
    ok(f"{app} compiled → {SW_BUILD}/main.elf")

# ── Step 5b: Flash programming (ZCU104) ───────────────────────────────────────

ICEPROG_DIR = SCRIPT_DIR / "hw/vendor/esl_epfl_x_heep/sw/vendor/yosyshq_icestorm/iceprog"

def build_iceprog() -> None:
    if not ICEPROG_DIR.exists():
        sys.exit(f"iceprog source not found at {ICEPROG_DIR}")
    result = subprocess.run(["make", "all"], cwd=ICEPROG_DIR, capture_output=True)
    if result.returncode != 0:
        sys.exit("Failed to build iceprog.")

def program_flash() -> None:
    header("Programming SPI flash (ZCU104)")
    info("Ensure all boot switches are OFF before programming (RISC-V must not be using flash)")
    build_iceprog()
    cmd = conda_cmd(["make", "flash-prog"])
    result = subprocess.run(cmd, env=ENV, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        sys.exit("make flash-prog failed — check that the bitstream is loaded and FT4232H is detected.")
    ok("SPI flash programmed")
    print()
    info("Next steps (manual):")
    info("  1. Set boot_select switch ON (SW1 switch 2) to enable flash boot")
    info("  2. Press the reset button to start the RISC-V from flash")
    info("  3. UART output will appear on FT4232H interface 2 (see step below)")
    input("Press Enter when the board has been reset and is running... ")


# ── Step 6: Start OpenOCD ──────────────────────────────────────────────────────

def _drain_stdout(proc: subprocess.Popen, prefix: str) -> None:
    """Daemon thread: drain proc.stdout to console so the pipe never fills up."""
    try:
        for line in proc.stdout:
            print(f"  [{prefix}] {line}", end="")
    except Exception:
        pass


def start_openocd(board: str, ext_jtag: bool = False) -> subprocess.Popen:
    header("Starting OpenOCD")
    config = BOARD_CONFIG[board]
    if ext_jtag and "openocd_cfg_ext" in config:
        cfg = config["openocd_cfg_ext"]
        info("Using external JTAG config (Pmod J87)")
    else:
        cfg = config["openocd_cfg"]
    cmd = ["openocd", "-f", str(cfg)]
    proc = _track(subprocess.Popen(
        cmd, env=ENV, cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    ))
    info(f"OpenOCD PID {proc.pid} — waiting for 'Ready for Remote Connections' ...")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                sys.exit(f"OpenOCD exited unexpectedly (rc={proc.returncode}).")
            continue
        print(f"  [openocd] {line}", end="")
        if "Ready for Remote Connections" in line:
            ok("OpenOCD ready")
            time.sleep(0.5)  # Allow target to fully initialize
            # Keep draining stdout so the pipe never blocks OpenOCD
            t = threading.Thread(target=_drain_stdout, args=(proc, "openocd"), daemon=True)
            t.start()
            return proc

    sys.exit("OpenOCD did not become ready within 60 s.")

# ── Step 7: Find UART port ─────────────────────────────────────────────────────

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
    for p in Path("/dev").glob("ttyUSB*"):
        print(f"  - {p}")
    port = input("Enter UART port manually (e.g. /dev/ttyUSB1): ").strip()
    if not port:
        sys.exit("No UART port specified.")
    return port

# ── Step 8: Open UART ──────────────────────────────────────────────────────────

def open_uart(port: str, board: str):
    """Return an open serial.Serial instance."""
    try:
        import serial
    except ImportError:
        sys.exit("pyserial is not installed. Run: pip install pyserial")
    config = BOARD_CONFIG[board]
    ser = serial.Serial(port, config["baud"], timeout=0.1)
    ok(f"UART opened: {port} @ {config['baud']} baud")
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
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    info(f"GDB PID {proc.pid}")
    time.sleep(0.5)
    _send("set remotetimeout 2000")
    _send("target remote localhost:3333")
    if reset_first:
        _send("monitor reset halt")
    _send("load")
    _send("continue")
    ok("GDB: commands sent (set remotetimeout / target remote / load / continue)")
    return proc

# ── Step 10: Capture UART output ───────────────────────────────────────────────

def capture_uart(ser, app: str, idle_timeout: int = UART_IDLE_TIMEOUT) -> str:
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
    lines = text.replace("\r", "").splitlines()
    result = []
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        if any(p in line for p in CYCLE_COUNT_PATTERNS):
            continue
        result.append(line)
    return result

def sim_output(app: str, simulator: str = "verilator") -> str:
    targets = {
        "verilator": ("run-verilator", "sim-verilator"),
        "questasim": ("run-questasim", "sim-modelsim"),
    }
    make_target, log_dir = targets[simulator]
    header(f"{simulator} simulation: {app}")
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
        description="FPGA workflow automation for HEEPsilon (PYNQ-Z1, PYNQ-Z2, ZCU104).",
    )
    p.add_argument("app",      metavar="NAME", nargs="?",
                   help="Application to run (skip interactive menu)")
    p.add_argument("--app",   metavar="NAME", dest="app_flag",
                   help=argparse.SUPPRESS)
    p.add_argument("--board", choices=list(BOARD_CONFIG.keys()), default="pynq-z2",
                   help="Target FPGA board (default: pynq-z2)")
    p.add_argument("--verify", action="store_true",
                   help="Also run in simulator and compare UART output line-by-line")
    p.add_argument("--sim", choices=["verilator", "questasim"], default="verilator",
                   help="Simulator to use with --verify (default: verilator)")
    flash_grp = p.add_mutually_exclusive_group()
    flash_grp.add_argument("--skip-program", action="store_true",
                   help="Skip bitstream programming (board already configured)")
    flash_grp.add_argument("--program", action="store_true",
                   help="Always program bitstream without prompting")
    p.add_argument("--uart-timeout", metavar="SECS", type=int, default=UART_IDLE_TIMEOUT,
                   help=f"UART idle timeout in seconds (default: {UART_IDLE_TIMEOUT})")
    p.add_argument("--gdb", action="store_true",
                   help="Force OpenOCD+GDB on-chip flow (ZCU104: requires BSCANE2 bitstream; "
                        "skips flash programming, uses LINKER=on_chip)")
    p.add_argument("--ext-jtag", action="store_true",
                   help="With --gdb: use external JTAG cable on Pmod J87 instead of BSCANE2 "
                        "(works with any bitstream; requires Digilent HS2 or similar FTDI cable)")
    return p

def main() -> None:
    os.chdir(SCRIPT_DIR)

    args = build_parser().parse_args()
    args.app = args.app or args.app_flag
    board = args.board

    openocd_proc: subprocess.Popen | None = None
    gdb_proc:     subprocess.Popen | None = None
    ser = None

    try:
        # 1. Board detection
        check_board(board)

        # 2. Bitstream check
        check_bitstream(board, gdb=args.gdb)

        # 3. Program bitstream
        if args.skip_program:
            info("Skipping bitstream programming (--skip-program)")
        elif args.program or input("Program bitstream to board? [Y/n] ").strip().lower() not in ("n", "no"):
            program_bitstream(board)
        else:
            info("Skipping bitstream programming")

        # 4. App selection
        app = pick_app(args.app)

        # 5. Build app
        build_app(app, board, force_on_chip=args.gdb)

        use_openocd = BOARD_CONFIG[board].get("use_openocd", True) or args.gdb

        if use_openocd:
            # On-chip flow (PYNQ boards): GDB loads the binary via OpenOCD
            # 6. OpenOCD
            openocd_proc = start_openocd(board, ext_jtag=getattr(args, "ext_jtag", False))

            # 7. UART
            uart_port = find_uart(board)

            # 8. Open UART
            header("Opening UART")
            ser = open_uart(uart_port, board)

            # 9. GDB
            gdb_proc = start_gdb(reset_first=args.skip_program)

        else:
            # Flash-load flow (ZCU104): iceprog burns the binary to SPI flash;
            # the RISC-V core boots from flash automatically after reset.
            # 5b. Program flash
            if not args.skip_program:
                program_flash()

            # 7. UART (FT4232H interface 2 = PL UART, no USB-TTL adapter needed)
            uart_port = find_uart(board)

            # 8. Open UART
            header("Opening UART")
            ser = open_uart(uart_port, board)

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
