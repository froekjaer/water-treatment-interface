"""
Headend ingest server — the receiving end for Edge telemetry.

Minimal stand-in for the future headend (which will grow out of the
TimeLapse Pro backend). It does the three things that matter for the
emulator chain:

1. AUTHENTICATES every request: HMAC-SHA256 over (timestamp + body) with a
   pre-shared key per Edge, plus a replay window (5 minutes).
2. STORES samples and heartbeats in SQLite (one row per sample, JSON kept
   intact so nothing is lost when the schema evolves).
3. SHOWS a small status page per plant: last contact, sample count,
   latest values — enough to verify the whole chain at a glance.

Usage:
    python headend/ingest_server.py --port 9090
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id TEXT NOT NULL,
    ts TEXT NOT NULL,            -- simulated plant time
    received_at TEXT NOT NULL,   -- real wall-clock time
    tank_level_pct REAL, pressure_bar REAL, consumption_m3h REAL,
    pump_running INTEGER, turbidity_clean_ntu REAL,
    payload TEXT NOT NULL,
    UNIQUE(edge_id, ts)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    edge_id TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    last_sim_ts TEXT
);
"""

# Pre-shared keys per Edge id. In the real system these live in the
# credential store (cf. TimeLapse Pro KeyManagementPage) — never in code.
EDGE_SECRETS = {
    "EDGE-PLANT-01": "dev-secret-change-me",
}

REPLAY_WINDOW_SECONDS = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def expected_signature(secret: str, timestamp: str, body: bytes) -> str:
    msg = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


STATUS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Waterworks Headend</title>
<meta http-equiv="refresh" content="5">
<style>
 body { font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; background:#f6f8fa; color:#1f2328; margin:0; }
 header { background:#0d4192; color:#fff; padding:14px 22px; font-weight:600; }
 main { max-width:960px; margin:20px auto; padding:0 16px; }
 .card { background:#fff; border:1px solid #d0d7de; border-radius:10px; padding:16px 18px; margin-bottom:14px; }
 .card h2 { font-size:15px; margin:0 0 8px; }
 .meta { color:#57606a; font-size:12px; margin-bottom:10px; }
 table { border-collapse:collapse; width:100%; font-size:13px; }
 td,th { border-bottom:1px solid #eaeef2; padding:6px 8px; text-align:left; }
 th { color:#57606a; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
 .on { color:#1a7f37; font-weight:600; } .off { color:#cf222e; font-weight:600; }
 .mono { font-family:ui-monospace,monospace; }
 .alarms { color:#cf222e; }
</style></head><body>
<header>💧 Waterworks Headend — ingest status</header>
<main>__CARDS__</main></body></html>"""

CARD = """
<div class="card">
  <h2>🏭 {edge_id} — <span class="{online_cls}">{online}</span></h2>
  <div class="meta">Last contact (wall clock): {last_seen} · Samples stored: {count} ·
      Latest plant time: <span class="mono">{last_ts}</span></div>
  <table><tr><th>Tower</th><th>Pressure</th><th>Demand</th><th>Pump</th><th>Turbidity</th><th>Alarms</th></tr>
  <tr><td>{tank}%</td><td>{pres} bar</td><td>{flow} m³/h</td><td>{pump}</td><td>{turb} NTU</td>
      <td class="alarms">{alarms}</td></tr></table>
</div>"""


class Headend:
    def __init__(self, db_path: Path) -> None:
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def ingest(self, edge_id: str, body: dict) -> dict:
        samples = body.get("samples", [])
        stored = 0
        last_ts = None
        for s in samples:
            try:
                self.db.execute(
                    """INSERT INTO samples
                       (edge_id, ts, received_at, tank_level_pct, pressure_bar,
                        consumption_m3h, pump_running, turbidity_clean_ntu, payload)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (edge_id, s["ts"], now_iso(),
                     s.get("tank_level_pct"), s.get("pressure_bar"),
                     s.get("consumption_m3h"), int(bool(s.get("pump_running"))),
                     s.get("turbidity_clean_ntu"), json.dumps(s)))
                stored += 1
            except sqlite3.IntegrityError:
                pass  # duplicate (edge_id, ts) — idempotent re-upload
            last_ts = s["ts"]
        if body.get("heartbeat"):
            self.db.execute(
                """INSERT INTO heartbeats (edge_id, last_seen, last_sim_ts)
                   VALUES (?,?,?)
                   ON CONFLICT(edge_id) DO UPDATE SET last_seen=excluded.last_seen,
                                                      last_sim_ts=excluded.last_sim_ts""",
                (edge_id, now_iso(), last_ts))
        self.db.commit()
        return {"ok": True, "stored": stored, "duplicates": len(samples) - stored}

    def status_html(self) -> str:
        cards = []
        edge_ids = [r[0] for r in self.db.execute(
            "SELECT DISTINCT edge_id FROM samples ORDER BY edge_id").fetchall()]
        hb = {r[0]: (r[1], r[2]) for r in self.db.execute(
            "SELECT edge_id, last_seen, last_sim_ts FROM heartbeats").fetchall()}
        for eid in sorted(set(edge_ids) | set(hb)):
            row = self.db.execute(
                """SELECT ts, tank_level_pct, pressure_bar, consumption_m3h,
                          pump_running, turbidity_clean_ntu, payload
                   FROM samples WHERE edge_id=? ORDER BY ts DESC LIMIT 1""",
                (eid,)).fetchone()
            count = self.db.execute(
                "SELECT COUNT(*) FROM samples WHERE edge_id=?", (eid,)).fetchone()[0]
            last_seen, _ = hb.get(eid, ("never", None))
            if row:
                alarms = json.loads(row[6]).get("active_alarms", [])
                card = CARD.format(
                    edge_id=eid, online="online", online_cls="on",
                    last_seen=last_seen, count=count, last_ts=row[0],
                    tank=row[1], pres=row[2], flow=row[3],
                    pump="running" if row[4] else "stopped",
                    turb=row[5], alarms=", ".join(alarms) or "—")
            else:
                card = CARD.format(
                    edge_id=eid, online="no data", online_cls="off",
                    last_seen=last_seen, count=0, last_ts="—",
                    tank="—", pres="—", flow="—", pump="—", turb="—", alarms="—")
            cards.append(card)
        inner = "".join(cards) or "<p>No Edge devices have reported yet.</p>"
        return STATUS_PAGE.replace("__CARDS__", inner)


class IngestHandler(BaseHTTPRequestHandler):
    headend: Headend  # set by serve()

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _json(self, obj, status=200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self, body: bytes) -> tuple[bool, str]:
        edge_id = self.headers.get("X-Edge-Id", "")
        ts = self.headers.get("X-Timestamp", "")
        sig = self.headers.get("X-Signature", "")
        secret = EDGE_SECRETS.get(edge_id)
        if not secret:
            return False, "unknown edge id"
        try:
            sent = datetime.fromisoformat(ts)
            skew = abs((datetime.now(timezone.utc) - sent).total_seconds())
            if skew > REPLAY_WINDOW_SECONDS:
                return False, f"timestamp outside replay window ({skew:.0f}s)"
        except ValueError:
            return False, "bad timestamp"
        if not hmac.compare_digest(expected_signature(secret, ts, body), sig):
            return False, "bad signature"
        return True, ""

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ingest":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ok, why = self._auth_ok(body)
        if not ok:
            self._json({"ok": False, "error": why}, status=401)
            print(f"[{now_iso()}] rejected ingest: {why}", flush=True)
            return
        edge_id = self.headers["X-Edge-Id"]
        result = self.headend.ingest(edge_id, json.loads(body))
        print(f"[{now_iso()}] {edge_id}: stored {result['stored']} sample(s)"
              + (f" ({result['duplicates']} dup)" if result["duplicates"] else ""))
        self._json(result)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            page = self.headend.status_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif path == "/api/latest":
            edge = parse_qs(urlparse(self.path).query).get("edge_id", ["EDGE-PLANT-01"])[0]
            row = self.headend.db.execute(
                "SELECT payload FROM samples WHERE edge_id=? ORDER BY ts DESC LIMIT 1",
                (edge,)).fetchone()
            self._json(json.loads(row[0]) if row else {"ok": False, "error": "no data"})
        else:
            self.send_error(404)


def serve(port: int = 9090, db_path: str | None = None) -> None:
    db = Path(db_path) if db_path else Path(__file__).parent / "headend.db"
    IngestHandler.headend = Headend(db)
    server = ThreadingHTTPServer(("127.0.0.1", port), IngestHandler)
    print(f"Headend ingest listening on http://localhost:{port}", flush=True)
    print(f"  status page: http://localhost:{port}/  (auto-refresh 5s)", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHeadend stopped.", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Waterworks headend ingest server")
    p.add_argument("--port", type=int, default=9090)
    p.add_argument("--db", default=None)
    args = p.parse_args()
    serve(port=args.port, db_path=args.db)
