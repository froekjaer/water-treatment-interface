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
emulator/        Physics core + control layer
  waterworks.py  The physics model (pump, tower, pressure, demand, quality, faults)
  plc.py         PLC emulator: scan cycle, Modbus-style register map,
                 interlocks, alarm evaluation, AUTO/MANUAL control
  hmi_server.py  HMI backend: runs physics+PLC at accelerated time,
                 serves JSON API + the HMI page (pure stdlib)
  demo_24h.py    Runs a simulated day and writes CSV + summary
hmi/
  static/
    index.html   The HMI page: live gauges, pump control, setpoints,
                 alarms with acknowledge, trend chart, fault injection
docs/            Design documents and simulation reports
```

## Getting started

**HMI (recommended):**

```bash
python emulator/hmi_server.py          # then open http://localhost:8080
python emulator/hmi_server.py --speed 60   # faster: one day in 24 min
```

The HMI shows the plant live: tower level, pressure, demand, turbidity,
pump control (AUTO/MANUAL), setpoints, alarms — and fault injection buttons
(leak, pump failure, frozen sensor) for demos and testing.

**Batch demo:**

```bash
python emulator/demo_24h.py
```

Runs a simulated day (1-minute samples) on a typical small plant and
writes `docs/sample_24h.csv` plus a summary to stdout.

## The PLC layer

`plc.py` emulates the programmable logic controller that would sit at a
real plant. Once per simulated minute it runs a classic scan cycle:
read inputs → execute logic → write outputs. Values are exposed through a
Modbus-inspired register map (input registers, holding registers, coils,
discrete inputs), so the HMI — and later the Edge agent — addresses the
plant the way a real SCADA system would. Interlocks (max runtime,
short-cycle protection) always win over the control strategy, and the PLC
runs its own sensor diagnostics (frozen-sensor detection).

## Roadmap

1. ✅ Emulator: physics core with pump, tower, pressure, demand, turbidity and fault scenarios
2. ✅ PLC emulator: scan cycle, register map, interlocks, alarms, AUTO/MANUAL
3. ✅ HMI: web UI with live process view, setpoints, alarms, trend, fault injection
4. ⬜ Emulator: simulated Edge agent posting telemetry to the headend
5. ⬜ Headend: ingest endpoints + time-series model (patterned after captures)
6. ⬜ UI: plant overview, trend charts, alarm panel (reuses Navbar/auth/GRC)
7. ⬜ Alarm and threshold engine on top of the existing notification setup

## Note

Real waterworks are critical infrastructure (NIS2 etc.). The emulator is a
risk-free sandbox — nothing here touches a real installation.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
