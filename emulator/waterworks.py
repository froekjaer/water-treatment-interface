"""
Physics core for the waterworks emulator.

Simulates a small waterworks:

    borehole -> borehole pump -> treatment (delay) -> water tower -> distribution network

The model is deliberately simple but plausible:
- Demand follows a diurnal profile (low at night, morning and evening peaks).
- The borehole pump is controlled by tower level hysteresis.
- Network pressure depends on demand and whether the pump is running.
- Turbidity drifts slowly and can spike (e.g. after pump starts).
- Fault scenarios can be injected: pump failure, leak, frozen sensor.

Pure Python with no dependencies, so the core runs anywhere —
including on an Edge device or in a CI test.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator, Optional


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass
class WaterworksConfig:
    """Settings for a small waterworks (typically 200-2000 consumers)."""

    # Water tower
    tank_capacity_m3: float = 100.0
    tank_start_level_m3: float = 60.0
    pump_start_level_m3: float = 35.0   # pump starts below this level
    pump_stop_level_m3: float = 85.0    # pump stops above this level

    # Borehole pump
    pump_capacity_m3h: float = 22.0     # nominal capacity
    pump_startup_minutes: int = 2       # ramp-up time before full capacity

    # Demand (m3/h) — the diurnal profile is scaled by these
    base_consumption_m3h: float = 4.0
    morning_peak_m3h: float = 14.0      # approx. 06:30-09:00
    evening_peak_m3h: float = 16.0      # approx. 17:00-21:00
    night_floor_m3h: float = 1.2        # 00:00-05:00 (leakage threshold!)

    # Pressure (bar)
    pressure_nominal_bar: float = 4.0
    pressure_drop_per_m3h: float = 0.045  # pressure loss per m3/h of demand
    pressure_noise_bar: float = 0.03

    # Turbidity (NTU)
    turbidity_baseline_ntu: float = 0.15
    turbidity_drift_ntu_per_day: float = 0.02
    turbidity_pumpstart_spike_ntu: float = 0.6  # sediment stirred up at pump start

    # Treatment: simple delay + turbidity reduction
    treatment_delay_minutes: int = 30
    treatment_removal_fraction: float = 0.55


@dataclass
class FaultScenario:
    """A scheduled fault scenario in the simulation."""
    kind: str                  # 'pump_failure' | 'leak' | 'sensor_freeze'
    start_minute: int          # minutes after simulation start
    duration_minutes: int
    leak_rate_m3h: float = 3.0            # only for 'leak'
    sensor: str = "tank_level"            # only for 'sensor_freeze'


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class WaterworksState:
    """Snapshot of the plant — this is what an Edge would report."""
    timestamp: datetime
    tank_level_m3: float
    tank_level_pct: float
    pump_running: bool
    pump_flow_m3h: float
    consumption_m3h: float
    network_pressure_bar: float
    turbidity_raw_ntu: float        # before treatment
    turbidity_clean_ntu: float      # after treatment (what consumers receive)
    daily_consumption_m3: float     # accumulated since midnight (simulated)
    active_faults: list[str] = field(default_factory=list)


# ── The model itself ─────────────────────────────────────────────────────────

class Waterworks:
    """Simulates one waterworks minute by minute."""

    def __init__(self, config: Optional[WaterworksConfig] = None,
                 faults: Optional[list[FaultScenario]] = None,
                 seed: Optional[int] = 42):
        self.cfg = config or WaterworksConfig()
        self.faults = faults or []
        self.rng = random.Random(seed)

        self.tank_m3 = self.cfg.tank_start_level_m3
        self.pump_on = False
        self.pump_minutes_running = 0
        self.pump_cycles_today = 0
        self.consumed_today = 0.0
        self._turbidity_raw = self.cfg.turbidity_baseline_ntu
        self._turbidity_spike_remaining = 0.0
        self._treatment_buffer: list[float] = []  # delay line for treatment
        self._frozen: dict[str, float] = {}       # sensor -> frozen value

        # External control: when True, the pump follows set_pump_command()
        # (used by the PLC layer) instead of the built-in hysteresis.
        self.external_control = False
        self._pump_command = False

    # -- external control interface (used by the PLC layer) -------------------

    def set_pump_command(self, on: bool) -> None:
        """Command the borehole pump on/off. Only honoured when
        external_control is True. The physics still applies ramp-up,
        and a pump_failure fault overrides any command."""
        self._pump_command = bool(on)

    def inject_fault(self, kind: str, start_minute: int, duration_minutes: int,
                     leak_rate_m3h: float = 3.0, sensor: str = "tank_level") -> None:
        """Inject a fault scenario at runtime (e.g. from the HMI)."""
        self.faults.append(FaultScenario(
            kind=kind, start_minute=start_minute, duration_minutes=duration_minutes,
            leak_rate_m3h=leak_rate_m3h, sensor=sensor,
        ))

    # -- internal helpers -----------------------------------------------------

    def _fault_active(self, kind: str, minute: int) -> bool:
        return any(f.kind == kind and f.start_minute <= minute < f.start_minute + f.duration_minutes
                   for f in self.faults)

    def _active_fault_names(self, minute: int) -> list[str]:
        return [f.kind for f in self.faults
                if f.start_minute <= minute < f.start_minute + f.duration_minutes]

    def _consumption_profile(self, t: datetime) -> float:
        """Diurnal demand profile (m3/h) with noise."""
        h = t.hour + t.minute / 60.0
        c = self.cfg
        # night floor
        flow = c.night_floor_m3h
        # morning peak (gaussian around 07:30)
        flow += c.morning_peak_m3h * math.exp(-((h - 7.5) ** 2) / (2 * 1.2 ** 2))
        # evening peak (gaussian around 19:00)
        flow += c.evening_peak_m3h * math.exp(-((h - 19.0) ** 2) / (2 * 1.6 ** 2))
        # weak daytime baseline outside the peaks
        flow += c.base_consumption_m3h * max(0.0, math.sin((h - 6) / 24 * 2 * math.pi))
        # ±10 % noise
        flow *= 1.0 + self.rng.uniform(-0.10, 0.10)
        return max(0.0, flow)

    def _update_pump(self, minute: int) -> None:
        """Pump state update.

        Two modes:
        - external_control=True: the pump follows the PLC's command
          (a pump_failure fault still forces it off — physics wins).
        - external_control=False: built-in hysteresis on tower level.
        """
        if self._fault_active("pump_failure", minute):
            if self.pump_on:
                self.pump_on = False
                self.pump_minutes_running = 0
            return

        if self.external_control:
            want = self._pump_command
        else:
            if not self.pump_on and self.tank_m3 < self.cfg.pump_start_level_m3:
                want = True
            elif self.pump_on and self.tank_m3 > self.cfg.pump_stop_level_m3:
                want = False
            else:
                want = self.pump_on

        if want and not self.pump_on:
            self.pump_on = True
            self.pump_minutes_running = 0
            self.pump_cycles_today += 1
            # sediment stirred up at pump start -> turbidity spike
            self._turbidity_spike_remaining += self.cfg.turbidity_pumpstart_spike_ntu
        elif not want and self.pump_on:
            self.pump_on = False
            self.pump_minutes_running = 0

    def _pump_flow(self) -> float:
        """Current pump output (m3/h) — ramps up during startup."""
        if not self.pump_on:
            return 0.0
        ramp = min(1.0, self.pump_minutes_running / max(1, self.cfg.pump_startup_minutes))
        return self.cfg.pump_capacity_m3h * ramp

    def _update_turbidity(self) -> None:
        """Raw turbidity: baseline + slow drift + decaying spikes."""
        drift = self.cfg.turbidity_drift_ntu_per_day / (24 * 60)
        self._turbidity_raw += drift + self.rng.uniform(-0.005, 0.005)
        if self._turbidity_spike_remaining > 0:
            take = min(self._turbidity_spike_remaining, 0.05)
            self._turbidity_raw += take
            self._turbidity_spike_remaining -= take
        self._turbidity_raw = max(0.02, self._turbidity_raw)

    def _treated_turbidity(self) -> float:
        """Treatment: delay + removal fraction, implemented as a buffer."""
        self._treatment_buffer.append(self._turbidity_raw)
        delay = self.cfg.treatment_delay_minutes
        if len(self._treatment_buffer) > delay:
            raw_in = self._treatment_buffer.pop(0)
        else:
            raw_in = self._treatment_buffer[0]
        return raw_in * (1.0 - self.cfg.treatment_removal_fraction)

    # -- main loop ------------------------------------------------------------

    def step(self, t: datetime, minute: int) -> WaterworksState:
        """One simulated minute. Returns the plant state."""
        # demand (+ possible leak: the plant outflow meter sees consumption + leak,
        # which is exactly the classic leak signal — elevated minimum night flow)
        consumption = self._consumption_profile(t)
        leak = 0.0
        if self._fault_active("leak", minute):
            leak = next(f.leak_rate_m3h for f in self.faults
                        if f.kind == "leak" and f.start_minute <= minute < f.start_minute + f.duration_minutes)
        outflow = consumption + leak

        # pump control and water balance
        self._update_pump(minute)
        pump_flow = self._pump_flow()
        if self.pump_on:
            self.pump_minutes_running += 1
        self.tank_m3 += (pump_flow - outflow) / 60.0
        self.tank_m3 = min(self.cfg.tank_capacity_m3, max(0.0, self.tank_m3))
        self.consumed_today += outflow / 60.0

        # pressure: nominal minus demand-dependent loss; leak adds extra loss
        pressure = (self.cfg.pressure_nominal_bar
                    - self.cfg.pressure_drop_per_m3h * consumption
                    - 0.15 * leak
                    + self.rng.uniform(-self.cfg.pressure_noise_bar, self.cfg.pressure_noise_bar))
        if self.tank_m3 <= 0.5:
            pressure = min(pressure, 0.8)  # empty tower = pressure failure

        # water quality
        self._update_turbidity()
        clean_ntu = self._treated_turbidity()

        # sensor freeze: hold the reported value while the fault is active
        tank_reported = self.tank_m3
        if self._fault_active("sensor_freeze", minute):
            if "tank_level" not in self._frozen:
                self._frozen["tank_level"] = self.tank_m3
            tank_reported = self._frozen["tank_level"]
        else:
            self._frozen.pop("tank_level", None)

        return WaterworksState(
            timestamp=t,
            tank_level_m3=round(tank_reported, 2),
            tank_level_pct=round(100 * tank_reported / self.cfg.tank_capacity_m3, 1),
            pump_running=self.pump_on,
            pump_flow_m3h=round(pump_flow, 2),
            consumption_m3h=round(outflow, 2),
            network_pressure_bar=round(pressure, 2),
            turbidity_raw_ntu=round(self._turbidity_raw, 3),
            turbidity_clean_ntu=round(clean_ntu, 3),
            daily_consumption_m3=round(self.consumed_today, 2),
            active_faults=self._active_fault_names(minute),
        )

    def simulate(self, start: datetime, minutes: int) -> Iterator[WaterworksState]:
        """Yield one state per simulated minute."""
        t = start
        for m in range(minutes):
            if t.hour == 0 and t.minute == 0:
                self.consumed_today = 0.0
                self.pump_cycles_today = 0
            yield self.step(t, m)
            t += timedelta(minutes=1)
