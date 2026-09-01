# Water Treatment Interface

Emulator of a small waterworks + interface layer, built as a pivot of the
[TimeLapse Pro](https://github.com/froekjaer/timelapse-pro) architecture:
same Edge/Headend model, hierarchy, user management, GRC and alarm engine —
but telemetry from a waterworks instead of images from a timelapse camera.

**Language policy: all code, documentation and commit messages in this
repository are written in English.**

## Purpose

1. **Emulator** — a physics-faithful simulation of a small waterworks
   (borehole pump → treatment → water tower → distribution network), so the
   entire platform can be developed and tested without access to a real plant.
2. **Interface** — Edge agent and headend API that receive telemetry
   (pressure, level, flow, water quality) the same way TimeLapse Pro
   receives captures today.

## Architecture mapping (TimeLapse Pro → waterworks)

| TimeLapse Pro | Waterworks |
|---|---|
| Customer → Site → Camera location | Utility → Waterworks → Measurement point/process line |
| Edge device (Orange Pi) | Edge device (PLC/gateway at the plant) |
| Captures (images) | Telemetry samples (pressure, level, flow, quality) |
| Timelapse video | Trend charts and plant overview |
| Drift analysis (focus/exposure) | Process analysis (leakage, pump cycles, night consumption) |
| Retention (images, GDPR) | Retention (metering data, logging requirements) |
| Alarm rules + SIEM | Process alarms (low pressure, high turbidity) |
| Config hierarchy Global→Customer→Site→Camera | Global→Utility→Plant→Measurement point |
| Break-glass / SSH / update flow | Reused unchanged |

## Repository layout (under construction)

```
emulator/        Physics core: simulation of the plant process
  waterworks.py  The model itself (pump, tower, pressure, demand, quality, faults)
  demo_24h.py    Runs a simulated day and writes CSV + summary
docs/            Design documents and simulation reports
```

## Getting started

```bash
python emulator/demo_24h.py
```

Runs a simulated day (1-minute samples) on a typical small plant and
writes `docs/sample_24h.csv` plus a summary to stdout.

## Roadmap

1. ✅ Emulator: physics core with pump, tower, pressure, demand, turbidity and fault scenarios
2. ⬜ Emulator: simulated Edge agent posting telemetry to the headend
3. ⬜ Headend: ingest endpoints + time-series model (patterned after captures)
4. ⬜ UI: plant overview, trend charts, alarm panel (reuses Navbar/auth/GRC)
5. ⬜ Alarm and threshold engine on top of the existing notification setup

## Note

Real waterworks are critical infrastructure (NIS2 etc.). The emulator is a
risk-free sandbox — nothing here touches a real installation.
