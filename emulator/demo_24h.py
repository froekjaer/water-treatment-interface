"""
Demo: run one simulated day on a small waterworks.

Writes docs/sample_24h.csv and prints a summary to stdout.

Usage:
    python emulator/demo_24h.py
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

# Allow running both as a module and as a plain script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from waterworks import FaultScenario, Waterworks  # noqa: E402

OUT_CSV = Path(__file__).resolve().parent.parent / "docs" / "sample_24h.csv"

FIELDS = [
    "timestamp", "tank_level_m3", "tank_level_pct", "pump_running",
    "pump_flow_m3h", "consumption_m3h", "network_pressure_bar",
    "turbidity_raw_ntu", "turbidity_clean_ntu", "daily_consumption_m3",
    "active_faults",
]


def main() -> None:
    start = datetime(2026, 9, 1, 0, 0)

    # Three fault scenarios during the day:
    faults = [
        # Leak in the distribution network 03:00-05:00 (visible as elevated night demand)
        FaultScenario(kind="leak", start_minute=3 * 60, duration_minutes=120, leak_rate_m3h=3.0),
        # Pump failure 13:00-14:30 (tower level drops, pressure follows)
        FaultScenario(kind="pump_failure", start_minute=13 * 60, duration_minutes=90),
        # Frozen tank level sensor 20:00-21:00 (reported value stuck)
        FaultScenario(kind="sensor_freeze", start_minute=20 * 60, duration_minutes=60),
    ]

    plant = Waterworks(faults=faults, seed=42)
    states = list(plant.simulate(start, minutes=24 * 60))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        for s in states:
            writer.writerow([
                s.timestamp.isoformat(), s.tank_level_m3, s.tank_level_pct,
                int(s.pump_running), s.pump_flow_m3h, s.consumption_m3h,
                s.network_pressure_bar, s.turbidity_raw_ntu,
                s.turbidity_clean_ntu, s.daily_consumption_m3,
                "|".join(s.active_faults),
            ])

    # ── Summary ──────────────────────────────────────────────────────────
    consumption = [s.consumption_m3h for s in states]
    pressure = [s.network_pressure_bar for s in states]
    tank = [s.tank_level_pct for s in states]
    turb_clean = [s.turbidity_clean_ntu for s in states]
    pump_minutes = sum(1 for s in states if s.pump_running)
    night = [s.consumption_m3h for s in states
             if 3 * 60 <= (s.timestamp.hour * 60 + s.timestamp.minute) < 5 * 60]

    print("=" * 64)
    print("Waterworks emulator — simulated day (2026-09-01)")
    print("=" * 64)
    print(f"Samples                : {len(states)} (1 per minute)")
    print(f"Total consumption      : {states[-1].daily_consumption_m3:.1f} m3")
    print(f"Peak demand            : {max(consumption):.1f} m3/h "
          f"at {states[consumption.index(max(consumption))].timestamp:%H:%M}")
    print(f"Night demand 03-05     : {sum(night)/len(night):.1f} m3/h avg "
          f"(leak active — compare with ~1.2 m3/h normal floor)")
    print(f"Pump runtime           : {pump_minutes} min "
          f"({pump_minutes / 60:.1f} h), cycles: {plant.pump_cycles_today}")
    print(f"Tower level            : {min(tank):.0f}% – {max(tank):.0f}%")
    print(f"Network pressure       : {min(pressure):.2f} – {max(pressure):.2f} bar")
    print(f"Turbidity (treated)    : {min(turb_clean):.2f} – {max(turb_clean):.2f} NTU")
    print(f"Fault scenarios        : {len(faults)} "
          f"(leak 03-05, pump failure 13:00-14:30, frozen sensor 20-21)")
    print(f"CSV written to         : {OUT_CSV.relative_to(Path.cwd()) if OUT_CSV.is_relative_to(Path.cwd()) else OUT_CSV}")
    print("=" * 64)


if __name__ == "__main__":
    main()
