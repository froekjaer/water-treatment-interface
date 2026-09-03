# Setting up on a new machine

Everything in this project runs from this repository alone — there is no
hidden local state worth migrating (the SQLite files are throwaway runtime
data). A new machine is just: clone, run.

## 1. Prerequisites

| Need | Check | Install if missing |
|---|---|---|
| git | `git --version` | `xcode-select --install` (macOS) |
| Python 3.10+ | `python3 --version` | `brew install python3` |
| Docker *(only for the OpenPLC Runtime)* | `docker --version` | `brew install --cask orbstack` or Docker Desktop |
| OpenPLC Editor *(only for editing the PLC program)* | — | https://autonomylogic.com/download |

The emulator itself is **pure Python standard library** — no `pip install`,
no virtualenv, no build step.

## 2. Clone and run

```bash
git clone https://github.com/froekjaer/water-treatment-interface.git
cd water-treatment-interface
python3 start_all.py
```

Open:

- **http://localhost:8090** — HMI: live process view, pump control,
  setpoints, alarms, trend, fault injection, live PLC ladder monitor
- **http://localhost:9090** — headend status page

Ctrl+C stops everything cleanly. `python3 start_all.py --speed 60` runs one
simulated day in 24 minutes.

## 3. Optional: this machine as the OpenPLC "PLC machine"

```bash
cd openplc
docker compose up -d        # OpenPLC Runtime v4 on https://localhost:8443
```

Then follow [openplc/README.md](openplc/README.md) for Editor connection,
I/O mapping and the starter program.

### Two-machine layout (PLC here, plant elsewhere)

If the waterworks physics should run on *another* machine (e.g. a Mac mini)
while this machine runs the PLC:

```bash
# on the machine that hosts the plant:
python3 emulator/modbus_bridge.py --speed 5     # Modbus TCP on port 5020

# on this machine: point the Editor's Modbus client mapping at
# <plant-machine-ip>:5020 as described in openplc/README.md
```

## 4. If you have a Kimi agent on the new machine

Kimi Work is local-first: conversations and workspaces do **not** sync
between machines. But you don't need the old conversation — this repository
*is* the shared state. Paste this to the agent on the new machine:

```text
Please clone https://github.com/froekjaer/water-treatment-interface into
my workspace, read README.md and SETUP-NEW-MACHINE.md, then run
"python3 start_all.py" and confirm that http://localhost:8090 (HMI) and
http://localhost:9090 (headend) both respond. The project is a small
waterworks emulator (physics + PLC emulator + HMI + edge agent + headend,
pure Python stdlib). If Docker is available, also read openplc/README.md
and tell me what is needed to run the OpenPLC Runtime on this machine.
```

The agent can take it from there — everything it needs is in the repo.
