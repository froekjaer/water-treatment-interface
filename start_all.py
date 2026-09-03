"""
start_all.py — start the whole emulator chain in one terminal.

Launches the plant (HMI), the headend and the edge agent as child
processes and merges their logs with coloured prefixes, so a demo or a
test run takes a single command:

    python start_all.py                 # default speed 5x
    python start_all.py --speed 60      # one day in 24 minutes

Open:
    http://localhost:8090   HMI (the plant, live control)
    http://localhost:9090   Headend status page

Press Ctrl+C once to stop everything cleanly.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# ANSI colours per component
COLORS = {"HMI": "\033[36m", "EDGE": "\033[33m", "HEADEND": "\033[35m"}
RESET = "\033[0m"


def pump(prefix: str, proc: subprocess.Popen) -> None:
    color = COLORS[prefix]
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"{color}[{prefix:7s}]{RESET} {line}", end="", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Start the full waterworks emulator chain")
    p.add_argument("--speed", type=int, default=5,
                   help="simulated minutes per real second (1-1440)")
    p.add_argument("--hmi-port", type=int, default=8090)
    p.add_argument("--headend-port", type=int, default=9090)
    args = p.parse_args()

    cmds = [
        ("HMI", [PY, str(ROOT / "emulator" / "hmi_server.py"),
                 "--port", str(args.hmi_port), "--speed", str(args.speed)]),
        ("HEADEND", [PY, str(ROOT / "headend" / "ingest_server.py"),
                     "--port", str(args.headend_port)]),
        ("EDGE", [PY, str(ROOT / "edge" / "edge_agent.py"),
                  "--plc", f"http://localhost:{args.hmi_port}",
                  "--headend", f"http://localhost:{args.headend_port}"]),
    ]

    procs: list[subprocess.Popen] = []
    print("=" * 64, flush=True)
    print("  Waterworks emulator — full chain starting", flush=True)
    print(f"  HMI:     http://localhost:{args.hmi_port}", flush=True)
    print(f"  Headend: http://localhost:{args.headend_port}", flush=True)
    print(f"  Speed:   {args.speed} sim-min/sec "
          f"(one day in {max(1, 24 * 60 // args.speed)} min)", flush=True)
    print("  Ctrl+C stops everything", flush=True)
    print("=" * 64, flush=True)

    for name, cmd in cmds:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        procs.append(proc)
        threading.Thread(target=pump, args=(name, proc), daemon=True).start()
        time.sleep(1.0)  # let each component bind its port before the next starts

    # Ctrl+C (SIGINT) already raises KeyboardInterrupt; make SIGTERM take the
    # same clean-shutdown path so no child processes are left behind.
    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    try:
        while True:
            time.sleep(0.5)
            for (name, _), proc in zip(cmds, procs):
                if proc.poll() is not None:
                    print(f"\n{COLORS[name]}[{name:7s}]{RESET} exited with code "
                          f"{proc.returncode} — shutting down the rest", flush=True)
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nStopping all components…", flush=True)
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("All stopped. 👋", flush=True)


if __name__ == "__main__":
    main()
