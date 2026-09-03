# OpenPLC integration — a real PLC for the emulated waterworks

This directory turns the emulator into the real OT architecture:

```
[modbus_bridge.py]          [OpenPLC Runtime v4]         [OpenPLC Editor]
 physics as Modbus TCP  ←→   runs YOUR ladder program  ←   write & upload
 "the plant + wired I/O"     (Docker, port 8443)            programs (macOS)
```

- **The plant** stays pure Python (`emulator/modbus_bridge.py`): physics +
  a Modbus TCP server exposing sensors as input registers and the pump
  command as a coil. No PLC logic lives here anymore.
- **The PLC** is OpenPLC Runtime v4 running in Docker on a separate machine
  (or the same one). It polls the bridge over Modbus TCP, executes the
  program with a real scan cycle, and writes the pump command back.
- **The Editor** (free, macOS) is where you write and modify the control —
  Ladder Diagram, Structured Text, FBD, etc. — and upload it to the Runtime.

> Note: with this setup the emulator's internal Python PLC (`plc.py`) is
> bypassed. The internal PLC + HMI ladder monitor remains the reference
> implementation; the five rungs you see in the HMI are exactly what
> `pump_control.st` implements.

## What you need

| Piece | Where | Install |
|---|---|---|
| OpenPLC Runtime v4 | the "PLC machine" (a Mac) | Docker, see below |
| OpenPLC Editor v4 | your own Mac | https://autonomylogic.com/download |
| modbus_bridge.py | anywhere reachable (e.g. the Mac mini) | this repo, Python 3 |

## 1. Start the Runtime (on the PLC machine)

Requires Docker Desktop or OrbStack (`brew install --cask orbstack`).

```bash
cd openplc
docker compose up -d
docker logs -f openplc-runtime     # wait for it to listen on 8443
```

The runtime generates a self-signed TLS certificate on first start and
listens on **https://&lt;machine&gt;:8443**. There is no browser UI — the
Editor is the UI.

## 2. Start the plant (the field device)

```bash
python emulator/modbus_bridge.py --speed 5
# Modbus TCP on port 5020, bound to 0.0.0.0 so the PLC machine can reach it
```

## 3. Connect the Editor

1. Install OpenPLC Editor v4 on your Mac and open it.
2. Add the runtime: `https://<plc-machine-ip>:8443` (accept the self-signed
   certificate; log in with the credentials shown in the runtime log —
   change them afterwards).
3. Create a new project, open `pump_control.st` from this directory and
   paste it into a Structured Text POU (or redraw the five rungs as Ladder
   Diagram — see the rung table in the file header).
4. Configure the **Modbus client** I/O mapping in the Editor so the
   program variables poll the bridge (`<mac-mini-ip>:5020`):

   | Variable | Modbus source | Meaning |
   |---|---|---|
   | `TankLevelPct` | input register 0 | tank level, % × 10 |
   | `PressureBar` | input register 2 | network pressure, bar × 100 |
   | `PumpFlowM3h` | input register 4 | pump flow, m³/h × 100 |
   | `TurbidityNtu` | input register 5 | turbidity, NTU × 1000 |
   | `PumpRunning` | discrete input 0 | pump feedback |
   | `PumpCmd` | coil 0 (write) | pump command |

5. Build, upload to the runtime, press **Start PLC** — and watch the pump
   follow your program. Use the Editor's live debug (WebSocket) to watch
   variables in real time.

## Try this

- Change `StartPct` / `StopPct`, re-upload, and watch the tank cycle change.
- Delete the seal-in rung (`M_KEEP`) and see the pump short-cycle.
- Tighten `MinOffMin` and watch the interlock protect the pump.

## Security notes (this is the NIS2 teaching moment)

- **Modbus has no authentication and no encryption.** Anything that can
  reach port 5020 can start the pump. On a real plant this is why OT
  networks are segmented and firewalled (IEC 62443 / NIS2).
- The Runtime v4 management API *is* secured (TLS + JWT) — a deliberate
  contrast worth discussing.
- Change the runtime's default credentials, and bind the bridge to a
  specific interface (`--host`) instead of `0.0.0.0` outside the lab.

## Troubleshooting

- `docker compose up` fails pulling the image → check Docker is running;
  the image is multi-arch (amd64 + arm64).
- Editor cannot connect → `curl -k https://<plc-ip>:8443/api/ping` should
  answer; check the Mac's firewall allows inbound 8443.
- PLC cannot reach the bridge → from the PLC machine:
  `nc -zv <mac-mini-ip> 5020`.
- Program compile errors in the Editor → `TIME * INT` multiplication is
  IEC 61131-3 standard, but if your Editor version complains, replace
  `PT := T#1m * MaxRuntimeMin` with a fixed `PT := T#240m` while testing.

## Files

```
docker-compose.yml   OpenPLC Runtime v4 container (official GHCR image)
pump_control.st      Starter program: the five rungs in Structured Text
../emulator/modbus_bridge.py   The plant as a Modbus TCP field device
```
