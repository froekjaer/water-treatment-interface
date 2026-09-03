"""
PLC emulator — local control layer for the waterworks.

Emulates a small programmable logic controller sitting between the physical
process (the Waterworks physics core) and the HMI/headend. It follows the
classic PLC scan cycle, executed once per simulated minute:

    1. READ INPUTS      — sensor values from the physics core
    2. EXECUTE LOGIC    — control strategy, interlocks, alarm evaluation
    3. WRITE OUTPUTS    — pump command back to the physics core

The register map is Modbus-inspired so the HMI (and later the Edge agent)
can address values the way a real SCADA system would:

Input registers (IR, read-only, scaled integers):
    IR0  tank level          [% x 10]
    IR1  tank level          [m3 x 100]
    IR2  network pressure    [bar x 100]
    IR3  demand (outflow)    [m3/h x 100]
    IR4  pump flow           [m3/h x 100]
    IR5  turbidity, treated  [NTU x 1000]
    IR6  turbidity, raw      [NTU x 1000]

Holding registers (HR, read/write setpoints):
    HR0  pump start level    [% x 10]      default 350 (35.0 %)
    HR1  pump stop level     [% x 10]      default 850 (85.0 %)
    HR2  low pressure alarm  [bar x 100]   default 250 (2.50 bar)
    HR3  high turbidity alarm[NTU x 1000]  default 500 (0.50 NTU)
    HR4  max pump runtime    [min]         default 240
    HR5  min pump off-time   [min]         default 10 (short-cycle protection)

Coils (C, read/write):
    C0   mode                1 = AUTO, 0 = MANUAL
    C1   manual pump command (only effective in MANUAL)

Discrete inputs (D, read-only status):
    D0   pump running
    D1   pump fault (commanded but not producing flow / failure active)
    D2   sensor fault (frozen tank level detected by the PLC itself)
    D3   interlock active (max runtime or min off-time enforcing)

Alarms are objects with id/text/severity/active/acknowledged — the HMI
acknowledges them, exactly like a real plant HMI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from waterworks import WaterworksState


# ── Alarms ───────────────────────────────────────────────────────────────────

@dataclass
class Alarm:
    id: str
    text: str
    severity: str               # 'critical' | 'warning'
    active: bool = False
    acknowledged: bool = False
    since_minute: Optional[int] = None


# ── The PLC ──────────────────────────────────────────────────────────────────

class PLC:
    """Scan-cycle controller for one waterworks."""

    def __init__(self) -> None:
        # Holding registers (setpoints)
        self.hr: dict[int, int] = {
            0: 350,    # pump start level  [% x 10]
            1: 850,    # pump stop level   [% x 10]
            2: 250,    # low pressure alarm [bar x 100]
            3: 500,    # high turbidity alarm [NTU x 1000]
            4: 240,    # max pump runtime [min]
            5: 10,     # min pump off-time [min]
        }
        # Coils
        self.auto_mode = True
        self.manual_pump_command = False

        # Internal logic state
        self.pump_command = False
        self._pump_on_minutes = 0
        self._pump_off_minutes = 0
        self._interlock_active = False
        self._low_pressure_scans = 0
        self._tank_readings: list[float] = []
        self._lad: dict = {}             # live ladder-monitor state (set by scan)

        self.alarms: dict[str, Alarm] = {
            "LOW_PRESSURE":   Alarm("LOW_PRESSURE",   "Network pressure below setpoint",        "critical"),
            "HIGH_TURBIDITY": Alarm("HIGH_TURBIDITY", "Treated water turbidity above setpoint", "critical"),
            "TANK_LOW":       Alarm("TANK_LOW",       "Water tower level low (< 15 %)",         "warning"),
            "TANK_HIGH":      Alarm("TANK_HIGH",      "Water tower level high (> 95 %)",        "warning"),
            "PUMP_FAULT":     Alarm("PUMP_FAULT",     "Pump commanded but not delivering",       "critical"),
            "SENSOR_FAULT":   Alarm("SENSOR_FAULT",   "Tank level sensor appears frozen",       "warning"),
            "INTERLOCK":      Alarm("INTERLOCK",      "Pump interlock active (runtime/off-time)","warning"),
        }

    # -- register access (Modbus-style) ---------------------------------------

    def read_input_registers(self, s: WaterworksState) -> dict[int, int]:
        return {
            0: int(round(s.tank_level_pct * 10)),
            1: int(round(s.tank_level_m3 * 100)),
            2: int(round(s.network_pressure_bar * 100)),
            3: int(round(s.consumption_m3h * 100)),
            4: int(round(s.pump_flow_m3h * 100)),
            5: int(round(s.turbidity_clean_ntu * 1000)),
            6: int(round(s.turbidity_raw_ntu * 1000)),
        }

    def read_holding_registers(self) -> dict[int, int]:
        return dict(self.hr)

    def write_holding_register(self, addr: int, value: int) -> None:
        if addr not in self.hr:
            raise KeyError(f"unknown holding register {addr}")
        if addr in (0, 1) and not (0 <= value <= 1000):
            raise ValueError("level setpoints must be 0-1000 (% x 10)")
        if addr == 0 and value >= self.hr[1]:
            raise ValueError("pump start level must be below stop level")
        if addr == 1 and value <= self.hr[0]:
            raise ValueError("pump stop level must be above start level")
        self.hr[addr] = int(value)

    def read_coils(self) -> dict[int, int]:
        return {0: int(self.auto_mode), 1: int(self.manual_pump_command)}

    def write_coil(self, addr: int, value: bool) -> None:
        if addr == 0:
            self.auto_mode = bool(value)
        elif addr == 1:
            self.manual_pump_command = bool(value)
        else:
            raise KeyError(f"unknown coil {addr}")

    def read_discrete_inputs(self, s: WaterworksState) -> dict[int, int]:
        return {
            0: int(s.pump_running),
            1: int(self.alarms["PUMP_FAULT"].active),
            2: int(self.alarms["SENSOR_FAULT"].active),
            3: int(self._interlock_active),
        }

    # -- alarm handling --------------------------------------------------------

    def _set_alarm(self, alarm_id: str, active: bool, minute: int) -> None:
        a = self.alarms[alarm_id]
        if active and not a.active:
            a.active = True
            a.acknowledged = False
            a.since_minute = minute
        elif not active and a.active:
            a.active = False
            a.since_minute = None

    def acknowledge(self, alarm_id: Optional[str] = None) -> None:
        for a in self.alarms.values():
            if alarm_id is None or a.id == alarm_id:
                a.acknowledged = True

    # -- the scan cycle ---------------------------------------------------------

    def scan(self, s: WaterworksState, minute: int) -> bool:
        """One PLC scan. Reads the latest physics state, runs logic, and
        returns the pump command to write back to the physics core."""

        # ── 1. READ INPUTS ────────────────────────────────────────────────
        ir = self.read_input_registers(s)
        level_pct = ir[0] / 10.0
        pressure_bar = ir[2] / 100.0
        turbidity_ntu = ir[5] / 1000.0
        pump_flow = ir[4] / 100.0

        # ── 2. EXECUTE LOGIC ──────────────────────────────────────────────

        # -- control strategy ------------------------------------------------
        prev_cmd = self.pump_command
        start_pct = self.hr[0] / 10.0
        stop_pct = self.hr[1] / 10.0
        if self.auto_mode:
            if not self.pump_command and level_pct < start_pct:
                want_pump = True
            elif self.pump_command and level_pct > stop_pct:
                want_pump = False
            else:
                want_pump = self.pump_command
        else:
            want_pump = self.manual_pump_command

        m_start = self.auto_mode and not prev_cmd and level_pct < start_pct
        m_keep = self.auto_mode and prev_cmd and level_pct <= stop_pct
        m_man = (not self.auto_mode) and self.manual_pump_command

        # -- interlocks (always win over the strategy) -----------------------
        self._interlock_active = False
        minoff_trip = False
        maxrun_trip = False
        if want_pump:
            if self._pump_off_minutes < self.hr[5] and not self.pump_command:
                want_pump = False                    # min off-time
                minoff_trip = True
                self._interlock_active = True
            elif self._pump_on_minutes >= self.hr[4]:
                want_pump = False                    # max continuous runtime
                maxrun_trip = True
                self._interlock_active = True

        # -- timers -----------------------------------------------------------
        if self.pump_command:
            self._pump_on_minutes += 1
            self._pump_off_minutes = 0
        else:
            self._pump_off_minutes += 1
            self._pump_on_minutes = 0

        self.pump_command = want_pump

        # -- live ladder-monitor state (rendered by the HMI PLC view) --------
        self._lad = {
            "auto": self.auto_mode,
            "manual_cmd": self.manual_pump_command,
            "level_pct": level_pct,
            "start_pct": start_pct,
            "stop_pct": stop_pct,
            "below_start": level_pct < start_pct,
            "above_stop": level_pct > stop_pct,
            "prev_cmd": prev_cmd,
            "m_start": m_start,
            "m_keep": m_keep,
            "m_man": m_man,
            "m_req": m_start or m_keep or m_man,
            "maxrun_trip": maxrun_trip,
            "minoff_trip": minoff_trip,
            "on_min": self._pump_on_minutes,
            "off_min": self._pump_off_minutes,
            "maxrun_sp": self.hr[4],
            "minoff_sp": self.hr[5],
            "out": self.pump_command,
        }

        # -- alarm evaluation --------------------------------------------------
        if pressure_bar < self.hr[2] / 100.0:
            self._low_pressure_scans += 1
        else:
            self._low_pressure_scans = 0
        self._set_alarm("LOW_PRESSURE", self._low_pressure_scans >= 3, minute)

        self._set_alarm("HIGH_TURBIDITY", turbidity_ntu > self.hr[3] / 1000.0, minute)
        self._set_alarm("TANK_LOW", level_pct < 15.0, minute)
        self._set_alarm("TANK_HIGH", level_pct > 95.0, minute)
        self._set_alarm("PUMP_FAULT",
                        self.pump_command and s.pump_running and pump_flow < 0.5
                        and self._pump_on_minutes > 3, minute)
        self._set_alarm("INTERLOCK", self._interlock_active, minute)

        # -- sensor diagnostics: frozen tank level ----------------------------
        self._tank_readings.append(round(s.tank_level_m3, 2))
        if len(self._tank_readings) > 30:
            self._tank_readings.pop(0)
        frozen = (len(self._tank_readings) == 30
                  and len(set(self._tank_readings)) == 1)
        self._set_alarm("SENSOR_FAULT", frozen, minute)

        # ── 3. WRITE OUTPUTS ──────────────────────────────────────────────
        return self.pump_command

    # -- convenience for the HMI ------------------------------------------------

    def alarm_list(self) -> list[dict]:
        return [
            {
                "id": a.id, "text": a.text, "severity": a.severity,
                "active": a.active, "acknowledged": a.acknowledged,
                "since_minute": a.since_minute,
            }
            for a in self.alarms.values()
        ]

    def ladder(self) -> dict:
        """Live ladder-logic view for the HMI PLC monitor.

        Returns the rung structure (mirroring the logic in scan()) with the
        live conducting state of every contact and coil, so the HMI can render
        power flow like the online view of a real PLC programming tool.
        For NC contacts, 'on' means power flows through (condition false).
        """
        if not self._lad:
            return {"rungs": []}
        L = self._lad

        def c(name: str, on: bool, value: str = "") -> dict:
            return {"kind": "no", "name": name, "on": bool(on), "value": value}

        def nc(name: str, on: bool, value: str = "") -> dict:
            return {"kind": "nc", "name": name, "on": bool(on), "value": value}

        def coil(name: str, on: bool, value: str = "") -> dict:
            return {"name": name, "on": bool(on), "value": value}

        return {
            "program": "PUMP_CONTROL",
            "rungs": [
                {"n": 1, "comment": "AUTO: start request when level < start setpoint",
                 "elements": [
                     c("AUTO", L["auto"]),
                     c("Lvl<Start", L["below_start"],
                       f"{L['level_pct']:.1f}% < {L['start_pct']:.1f}%"),
                     nc("PumpCmd", not L["prev_cmd"],
                        "pump stopped" if not L["prev_cmd"] else "pump running"),
                 ],
                 "coil": coil("M_START", L["m_start"], "start request")},
                {"n": 2, "comment": "AUTO: seal-in — keep running until stop level",
                 "elements": [
                     c("AUTO", L["auto"]),
                     c("PumpCmd", L["prev_cmd"]),
                     nc("Lvl>Stop", not L["above_stop"],
                        f"{L['level_pct']:.1f}% > {L['stop_pct']:.1f}%"),
                 ],
                 "coil": coil("M_KEEP", L["m_keep"], "keep running")},
                {"n": 3, "comment": "MANUAL: operator pump command",
                 "elements": [
                     nc("AUTO", not L["auto"],
                        "AUTO" if L["auto"] else "MANUAL"),
                     c("ManualCmd", L["manual_cmd"]),
                 ],
                 "coil": coil("M_MAN", L["m_man"], "manual request")},
                {"n": 4, "comment": "Pump request = start OR keep OR manual",
                 "branch": [
                     [c("M_START", L["m_start"])],
                     [c("M_KEEP", L["m_keep"])],
                     [c("M_MAN", L["m_man"])],
                 ],
                 "coil": coil("M_REQ", L["m_req"], "pump request")},
                {"n": 5, "comment": "Interlocks always win: max runtime + short-cycle protection",
                 "elements": [
                     c("M_REQ", L["m_req"]),
                     nc("MaxRun", not L["maxrun_trip"],
                        f"on {L['on_min']}/{L['maxrun_sp']} min"),
                     nc("MinOff", not L["minoff_trip"],
                        f"off {L['off_min']}/{L['minoff_sp']} min"),
                 ],
                 "coil": coil("Q0 PumpCmd", L["out"], "physical output")},
            ],
        }
