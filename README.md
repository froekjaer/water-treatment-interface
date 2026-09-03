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
edge/
  edge_agent.py  Simulated on-site gateway: polls the PLC, buffers samples
                 in a local SQLite outbox (store-and-forward), uploads
                 batches to the headend with HMAC-signed requests
headend/
  ingest_server.py  Minimal headend: authenticates (HMAC + replay window),
                 stores samples in SQLite, shows a per-plant status page
hmi/
  static/
    index.html   The HMI page: live gauges, pump control, setpoints,
                 alarms with acknowledge, trend chart, fault injection
docs/            Design documents and simulation reports
```

## Getting started

**The full chain (PLC → Edge → Headend), three terminals:**

```bash
python emulator/hmi_server.py          # 1: the plant (HMI on :8090)
python headend/ingest_server.py        # 2: the headend (status on :9090)
python edge/edge_agent.py              # 3: the edge agent (links them)
```

Then open:
- **http://localhost:8090** — the HMI: live process view, pump control,
  setpoints, alarms, trend chart, fault injection buttons
- **http://localhost:9090** — the headend status page: what the central
  system has actually received (auto-refreshes)

Try killing the headend (Ctrl+C in terminal 2) while the others run:
the Edge agent keeps every sample in its outbox and flushes the backlog
when the headend returns — exactly like the real store-and-forward design.

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
4. ✅ Simulated Edge agent: PLC polling, store-and-forward outbox, HMAC-signed upload
5. ✅ Minimal headend stand-in: authenticated ingest, SQLite storage, status page
6. ⬜ Real headend: grow ingest into the timelapse-derived backend (time-series model, auth, users)
7. ⬜ UI: plant overview, trend charts, alarm panel (reuses Navbar/auth/GRC)
8. ⬜ Alarm and threshold engine on top of the existing notification setup

## Note

Real waterworks are critical infrastructure (NIS2 etc.). The emulator is a
risk-free sandbox — nothing here touches a real installation.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
