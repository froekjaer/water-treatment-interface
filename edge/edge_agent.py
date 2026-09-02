"""
Edge agent — the simulated on-site gateway.

Sits at the plant, next to the PLC. It mirrors what the TimeLapse Pro Edge
does for cameras, but for telemetry:

    PLC (local, trusted)  -->  Edge agent  -->  Headend (over the network)

Behaviour modelled after the real thing:
- Polls the PLC on a fixed interval and maps its registers to samples.
- Store-and-forward: samples are written to a local SQLite outbox BEFORE
  any upload attempt, so nothing is lost during network outages.
- Uploads in batches on a sync interval, with exponential backoff on failure.
- Each request is authenticated with a pre-shared key (HMAC-SHA256 over the
  body + timestamp), like the mTLS/token scheme in TimeLapse Pro.
- Sends a heartbeat with every sync so the headend can tell "online but
  nothing new" from "offline".

Usage:
    python edge/edge_agent.py \
        --plc http://localhost:8080 \
        --headend http://localhost:9090 \
        --edge-id EDGE-PLANT-01 --secret dev-secret-change-me
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,           -- simulated plant time (ISO)
    payload TEXT NOT NULL       -- JSON sample
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class EdgeAgent:
    def __init__(self, plc_url: str, headend_url: str, edge_id: str,
                 secret: str, outbox_path: Path,
                 poll_seconds: float = 2.0, sync_seconds: float = 15.0) -> None:
        self.plc_url = plc_url.rstrip("/")
        self.headend_url = headend_url.rstrip("/")
        self.edge_id = edge_id
        self.secret = secret
        self.poll_seconds = poll_seconds
        self.sync_seconds = sync_seconds
        self.backoff = 1.0
        self.last_seen_sim_ts: str | None = None
        self.db = sqlite3.connect(str(outbox_path))
        self.db.executescript(OUTBOX_SCHEMA)
        self.db.commit()

    # -- local outbox ----------------------------------------------------------

    def enqueue(self, ts: str, sample: dict) -> None:
        self.db.execute("INSERT INTO outbox (ts, payload) VALUES (?, ?)",
                        (ts, json.dumps(sample)))
        self.db.commit()

    def pending(self) -> list[tuple[int, str]]:
        rows = self.db.execute(
            "SELECT id, payload FROM outbox ORDER BY id LIMIT 500").fetchall()
        return rows

    def drop_through(self, last_id: int) -> None:
        self.db.execute("DELETE FROM outbox WHERE id <= ?", (last_id,))
        self.db.commit()

    # -- PLC polling -------------------------------------------------------------

    def poll_plc(self) -> dict | None:
        """Read the current PLC state and convert it to a telemetry sample."""
        try:
            with urllib.request.urlopen(self.plc_url + "/api/state", timeout=5) as r:
                state = json.load(r)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"[{now_iso()}] PLC poll failed: {e}", flush=True)
            return None

        sim_ts = state["sim_time"]
        if sim_ts == self.last_seen_sim_ts:
            return None                     # no new simulated minute yet
        self.last_seen_sim_ts = sim_ts

        ir = state["registers"]["IR"]
        return {
            "ts": sim_ts,
            "edge_id": self.edge_id,
            "tank_level_pct": state["tank_level_pct"],
            "tank_level_m3": state["tank_level_m3"],
            "pressure_bar": state["pressure_bar"],
            "consumption_m3h": state["consumption_m3h"],
            "pump_flow_m3h": state["pump_flow_m3h"],
            "pump_running": state["pump_running"],
            "auto_mode": state["auto_mode"],
            "turbidity_clean_ntu": state["turbidity_clean_ntu"],
            "daily_consumption_m3": state["daily_consumption_m3"],
            "active_faults": state["active_faults"],
            "active_alarms": [a["id"] for a in state["alarms"] if a["active"]],
            "registers_raw": {"IR": ir, "D": state["registers"]["D"]},
        }

    # -- headend upload -----------------------------------------------------------

    def _post(self, path: str, body_obj: dict) -> bool:
        body = json.dumps(body_obj).encode()
        ts = now_iso()
        req = urllib.request.Request(
            self.headend_url + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Edge-Id": self.edge_id,
                "X-Timestamp": ts,
                "X-Signature": sign(self.secret, ts, body),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, OSError) as e:
            print(f"[{now_iso()}] upload to headend failed: {e}", flush=True)
            return False

    def sync_once(self) -> None:
        rows = self.pending()
        batch = [json.loads(payload) for _, payload in rows]
        ok = self._post("/api/ingest", {
            "edge_id": self.edge_id,
            "sent_at": now_iso(),
            "samples": batch,
            "heartbeat": True,
            "pending_after": 0,  # updated below on success
        })
        if ok:
            if rows:
                self.drop_through(rows[-1][0])
                print(f"[{now_iso()}] uploaded {len(rows)} sample(s), outbox empty", flush=True)
            else:
                print(f"[{now_iso()}] heartbeat (no new samples)", flush=True)
            self.backoff = 1.0
        else:
            self.backoff = min(self.backoff * 2, 300)
            print(f"[{now_iso()}] {len(rows)} sample(s) kept in outbox, "
                  f"retry in {self.backoff:.0f}s")

    # -- main loop -----------------------------------------------------------------

    def run(self) -> None:
        print(f"[{now_iso()}] Edge agent {self.edge_id} starting", flush=True)
        print(f"  PLC:     {self.plc_url}", flush=True)
        print(f"  Headend: {self.headend_url}", flush=True)
        print(f"  Outbox:  store-and-forward SQLite", flush=True)
        next_sync = 0.0
        while True:
            sample = self.poll_plc()
            if sample:
                self.enqueue(sample["ts"], sample)
            if time.monotonic() >= next_sync:
                self.sync_once()
                next_sync = time.monotonic() + max(self.sync_seconds, self.backoff)
            time.sleep(self.poll_seconds)


def main() -> None:
    p = argparse.ArgumentParser(description="Waterworks Edge agent (simulated)")
    p.add_argument("--plc", default="http://localhost:8080",
                   help="Base URL of the plant PLC/HMI")
    p.add_argument("--headend", default="http://localhost:9090",
                   help="Base URL of the headend ingest server")
    p.add_argument("--edge-id", default="EDGE-PLANT-01")
    p.add_argument("--secret", default="dev-secret-change-me",
                   help="Pre-shared key for HMAC request signing")
    p.add_argument("--outbox", default=str(Path(__file__).parent / "outbox.db"))
    p.add_argument("--poll-seconds", type=float, default=2.0)
    p.add_argument("--sync-seconds", type=float, default=15.0)
    args = p.parse_args()

    agent = EdgeAgent(
        plc_url=args.plc, headend_url=args.headend,
        edge_id=args.edge_id, secret=args.secret,
        outbox_path=Path(args.outbox),
        poll_seconds=args.poll_seconds, sync_seconds=args.sync_seconds,
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        print("\nEdge agent stopped.", flush=True)


if __name__ == "__main__":
    main()
