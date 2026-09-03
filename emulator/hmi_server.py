"""
HMI server — web-based Human-Machine Interface for the waterworks emulator.

Runs the physics core + PLC in a background thread at accelerated time and
serves a small JSON API plus a static HMI page. Pure standard library —
no dependencies, runs anywhere Python runs.

Usage:
    python emulator/hmi_server.py [--port 8090] [--speed 5]

Then open http://localhost:8090 in a browser.

Speed is simulated minutes per real second (default 5 → one day in ~5 hours;
use 60 for a day in 24 minutes, 1440 for a day in 1 minute).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from plc import PLC                      # noqa: E402
from waterworks import Waterworks        # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent.parent / "hmi" / "static"
HISTORY_LEN = 24 * 60  # one full day of 1-minute samples


class Plant:
    """Physics + PLC + simulation clock, guarded by a lock."""

    def __init__(self, speed: int = 5) -> None:
        self.lock = threading.Lock()
        self.physics = Waterworks(seed=42)
        self.physics.external_control = True
        self.plc = PLC()
        self.sim_time = datetime(2026, 9, 1, 0, 0)
        self.minute = 0
        self.speed = speed                # simulated minutes per real second
        self.history: deque[dict] = deque(maxlen=HISTORY_LEN)
        self.last_state: dict | None = None

    def step_once(self) -> None:
        """Advance one simulated minute (called under lock)."""
        state = self.physics.step(self.sim_time, self.minute)
        command = self.plc.scan(state, self.minute)
        self.physics.set_pump_command(command)
        self.sim_time += timedelta(minutes=1)
        self.minute += 1
        if self.sim_time.hour == 0 and self.sim_time.minute == 0:
            self.physics.consumed_today = 0.0
            self.physics.pump_cycles_today = 0
        self.last_state = self.snapshot(state)
        self.history.append(self.last_state)

    def snapshot(self, s) -> dict:
        plc = self.plc
        return {
            "sim_time": self.sim_time.isoformat(timespec="minutes"),
            "minute": self.minute,
            "speed": self.speed,
            "tank_level_pct": s.tank_level_pct,
            "tank_level_m3": s.tank_level_m3,
            "pressure_bar": s.network_pressure_bar,
            "consumption_m3h": s.consumption_m3h,
            "pump_flow_m3h": s.pump_flow_m3h,
            "pump_running": s.pump_running,
            "pump_command": plc.pump_command,
            "auto_mode": plc.auto_mode,
            "turbidity_clean_ntu": s.turbidity_clean_ntu,
            "turbidity_raw_ntu": s.turbidity_raw_ntu,
            "daily_consumption_m3": s.daily_consumption_m3,
            "pump_cycles_today": self.physics.pump_cycles_today,
            "active_faults": s.active_faults,
            "alarms": plc.alarm_list(),
            "registers": {
                "IR": plc.read_input_registers(s),
                "HR": plc.read_holding_registers(),
                "C": plc.read_coils(),
                "D": plc.read_discrete_inputs(s),
            },
        }

    def run_forever(self) -> None:
        """Background loop: advance `speed` simulated minutes per real second."""
        while True:
            tick_start = time.monotonic()
            with self.lock:
                for _ in range(max(1, self.speed)):
                    self.step_once()
            elapsed = time.monotonic() - tick_start
            time.sleep(max(0.05, 1.0 - elapsed))

    # -- commands from the HMI -------------------------------------------------

    def handle_command(self, cmd: dict[str, Any]) -> dict:
        action = cmd.get("action")
        with self.lock:
            if action == "set_mode":
                self.plc.write_coil(0, bool(cmd.get("auto")))
            elif action == "pump_manual":
                self.plc.write_coil(1, bool(cmd.get("on")))
            elif action == "set_setpoint":
                self.plc.write_holding_register(int(cmd["register"]), int(cmd["value"]))
            elif action == "ack_alarm":
                self.plc.acknowledge(cmd.get("id"))
            elif action == "ack_all":
                self.plc.acknowledge()
            elif action == "set_speed":
                self.speed = max(1, min(1440, int(cmd["speed"])))
            elif action == "inject_fault":
                self.physics.inject_fault(
                    kind=str(cmd["kind"]),
                    start_minute=self.minute + 1,
                    duration_minutes=int(cmd.get("duration_minutes", 60)),
                    leak_rate_m3h=float(cmd.get("leak_rate_m3h", 3.0)),
                )
            else:
                return {"ok": False, "error": f"unknown action {action!r}"}
            return {"ok": True, "state": self.last_state}


# ── HTTP layer ────────────────────────────────────────────────────────────────

class HmiHandler(BaseHTTPRequestHandler):
    plant: Plant  # set by serve()

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        pass

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/api/state":
            with self.plant.lock:
                self._json(self.plant.last_state)
        elif url.path == "/api/history":
            qs = parse_qs(url.query)
            points = min(int(qs.get("points", ["240"])[0]), HISTORY_LEN)
            with self.plant.lock:
                data = list(self.plant.history)[-points:]
            self._json({"samples": data})
        elif url.path == "/api/ladder":
            with self.plant.lock:
                self._json(self.plant.plc.ladder())
        elif url.path == "/" or url.path == "/index.html":
            self._static("index.html", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/command":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            cmd = json.loads(self.rfile.read(length) or b"{}")
            self._json(self.plant.handle_command(cmd))
        except (ValueError, KeyError) as e:
            self._json({"ok": False, "error": str(e)}, status=400)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8090, speed: int = 5) -> None:
    plant = Plant(speed=speed)
    with plant.lock:
        plant.step_once()  # prime first state
    threading.Thread(target=plant.run_forever, daemon=True).start()
    HmiHandler.plant = plant
    server = ThreadingHTTPServer(("127.0.0.1", port), HmiHandler)
    print(f"HMI running at http://localhost:{port}  "
          f"(speed: {speed} sim-minutes/sec — one day in {24*60//speed} min)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Waterworks HMI server")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--speed", type=int, default=5,
                   help="simulated minutes per real second (1-1440)")
    args = p.parse_args()
    serve(port=args.port, speed=args.speed)
