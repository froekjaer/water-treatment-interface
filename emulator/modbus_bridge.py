"""
modbus_bridge.py — virtual field device for the OpenPLC integration.

Runs the waterworks physics core and exposes it as a Modbus TCP server, so
a real PLC runtime (e.g. OpenPLC Runtime v4 in Docker) can take over the
control logic. The PLC polls sensor values from input registers and writes
the pump command to coil C0 — exactly like wired I/O on a real plant.

In this mode the internal Python PLC (plc.py) is NOT running: the OpenPLC
program is the controller. This process is "the plant + its wired I/O".

Pure standard library — no dependencies.

Usage:
    python emulator/modbus_bridge.py [--port 5020] [--speed 5]

Register map (same scaling as emulator/plc.py):

Input registers (FC04, read-only):
    IR0  tank level          [% x 10]
    IR1  tank level          [m3 x 100]
    IR2  network pressure    [bar x 100]
    IR3  demand (outflow)    [m3/h x 100]
    IR4  pump flow           [m3/h x 100]
    IR5  turbidity, treated  [NTU x 1000]
    IR6  turbidity, raw      [NTU x 1000]

Coils (FC01 read / FC05 write):
    C0   pump command (1 = run) — written by the PLC

Discrete inputs (FC02, read-only):
    D0   pump running (physical feedback from the process)

Holding registers (FC03 read / FC06 write):
    HR0  pump start level    [% x 10]      default 350
    HR1  pump stop level     [% x 10]      default 850
    (setpoints live here if the PLC program prefers reading them over Modbus;
     a PLC program may equally well keep its own setpoints internally)

Supported function codes: 1, 2, 3, 4, 5, 6. Others get exception 01.
"""

from __future__ import annotations

import argparse
import socketserver
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from waterworks import Waterworks        # noqa: E402

# Exception codes
ILLEGAL_FUNCTION = 0x01
ILLEGAL_ADDRESS = 0x02
ILLEGAL_VALUE = 0x03


class FieldDevice:
    """Physics + register banks, guarded by a lock."""

    def __init__(self, speed: int = 5) -> None:
        self.lock = threading.Lock()
        self.physics = Waterworks(seed=42)
        self.physics.external_control = True
        self.sim_time = datetime(2026, 9, 1, 0, 0)
        self.minute = 0
        self.speed = speed                     # simulated minutes per real second
        self.coils = {0: False}                # C0: pump command (from the PLC)
        self.hr = {0: 350, 1: 850}             # optional setpoints

    def step_once(self) -> None:
        state = self.physics.step(self.sim_time, self.minute)
        self._state = state
        self.physics.set_pump_command(self.coils[0])
        self.sim_time += timedelta(minutes=1)
        self.minute += 1

    def run_forever(self) -> None:
        while True:
            t0 = time.monotonic()
            with self.lock:
                for _ in range(max(1, self.speed)):
                    self.step_once()
            time.sleep(max(0.05, 1.0 - (time.monotonic() - t0)))

    # -- register banks ---------------------------------------------------------

    def input_registers(self) -> dict[int, int]:
        s = self._state
        return {
            0: int(round(s.tank_level_pct * 10)),
            1: int(round(s.tank_level_m3 * 100)),
            2: int(round(s.network_pressure_bar * 100)),
            3: int(round(s.consumption_m3h * 100)),
            4: int(round(s.pump_flow_m3h * 100)),
            5: int(round(s.turbidity_clean_ntu * 1000)),
            6: int(round(s.turbidity_raw_ntu * 1000)),
        }

    def discrete_inputs(self) -> dict[int, int]:
        return {0: int(self._state.pump_running)}


# ── Modbus TCP protocol ───────────────────────────────────────────────────────

def _bits(values: list[int]) -> bytes:
    """Pack coil/discrete values LSB-first into bytes (Modbus convention)."""
    out = bytearray((len(values) + 7) // 8)
    for i, v in enumerate(values):
        if v:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


class ModbusHandler(socketserver.BaseRequestHandler):
    device: FieldDevice  # set by serve()

    def _recvn(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def handle(self) -> None:
        while True:
            hdr = self._recvn(7)
            if hdr is None:
                return
            tid, pid, length, uid = struct.unpack(">HHHB", hdr)
            body = self._recvn(length - 1)
            if body is None or pid != 0:
                return
            pdu = self._execute(body[0], body[1:])
            resp = struct.pack(">HHHB", tid, 0, len(pdu) + 1, uid) + pdu
            self.request.sendall(resp)

    def _execute(self, fc: int, data: bytes) -> bytes:
        dev = self.device
        try:
            if fc in (3, 4):  # read holding / input registers
                addr, cnt = struct.unpack(">HH", data[:4])
                if not 1 <= cnt <= 125:
                    return bytes([fc | 0x80, ILLEGAL_VALUE])
                with dev.lock:
                    bank = dev.hr if fc == 3 else dev.input_registers()
                    vals = []
                    for a in range(addr, addr + cnt):
                        if a not in bank:
                            return bytes([fc | 0x80, ILLEGAL_ADDRESS])
                        vals.append(bank[a])
                return bytes([fc, cnt * 2]) + struct.pack(f">{cnt}H", *vals)

            if fc in (1, 2):  # read coils / discrete inputs
                addr, cnt = struct.unpack(">HH", data[:4])
                if not 1 <= cnt <= 2000:
                    return bytes([fc | 0x80, ILLEGAL_VALUE])
                with dev.lock:
                    bank = dev.coils if fc == 1 else dev.discrete_inputs()
                    vals = []
                    for a in range(addr, addr + cnt):
                        if a not in bank:
                            return bytes([fc | 0x80, ILLEGAL_ADDRESS])
                        vals.append(int(bank[a]))
                packed = _bits(vals)
                return bytes([fc, len(packed)]) + packed

            if fc == 5:       # write single coil
                addr, raw = struct.unpack(">HH", data[:4])
                if raw not in (0x0000, 0xFF00):
                    return bytes([fc | 0x80, ILLEGAL_VALUE])
                with dev.lock:
                    if addr not in dev.coils:
                        return bytes([fc | 0x80, ILLEGAL_ADDRESS])
                    dev.coils[addr] = raw == 0xFF00
                return bytes([fc]) + data[:4]   # echo

            if fc == 6:       # write single holding register
                addr, val = struct.unpack(">HH", data[:4])
                with dev.lock:
                    if addr not in dev.hr:
                        return bytes([fc | 0x80, ILLEGAL_ADDRESS])
                    dev.hr[addr] = val
                return bytes([fc]) + data[:4]   # echo

            return bytes([fc | 0x80, ILLEGAL_FUNCTION])
        except (struct.error, IndexError):
            return bytes([fc | 0x80, ILLEGAL_VALUE])


class ModbusServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 5020, speed: int = 5, host: str = "0.0.0.0") -> None:
    dev = FieldDevice(speed=speed)
    with dev.lock:
        dev.step_once()  # prime first state
    threading.Thread(target=dev.run_forever, daemon=True).start()
    ModbusHandler.device = dev
    server = ModbusServer((host, port), ModbusHandler)
    print(f"Modbus field device listening on {host}:{port}  "
          f"(speed: {speed} sim-min/sec)")
    print("Point the OpenPLC Runtime Modbus client at this host/port.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Waterworks Modbus field device")
    p.add_argument("--port", type=int, default=5020,
                   help="Modbus TCP port (502 requires root; default 5020)")
    p.add_argument("--host", default="0.0.0.0",
                   help="bind address (default 0.0.0.0 so the PLC machine can reach it)")
    p.add_argument("--speed", type=int, default=5,
                   help="simulated minutes per real second (1-1440)")
    args = p.parse_args()
    serve(port=args.port, speed=args.speed, host=args.host)
