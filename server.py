#!/usr/bin/env python3
"""server.py -- Shack-Track web server (weekend-one item 2, read-only half).

Serves the Concept B pass list and two JSON endpoints. No radio, no rotator, no
WebSocket yet: everything here is computed from a TLE file and the QTH, so it is
safe to expose and useful with the shack switched off.

    GET /                 the pass list (static/index.html)
    GET /api/passes       next N hours of passes, ?hours=12 (1..48)
    GET /api/keps         keps-watch.py --json, so staleness is visible on the page
    GET /api/health       what this server is reading and how old it is

Configuration is by environment so the same file runs on aurora13 for a look and
on shack-hub for real:

    SHACKTRACK_TLE     TLE file          (default: amateur.tle beside this script)
    SHACKTRACK_SATS    satellites.json   (default: beside this script)
    SHACKTRACK_KEPS    keps-watch.py     (default: beside this script; "" disables)
    SHACKTRACK_PORT    listen port       (default 8781)
    SHACKTRACK_BIND    listen address    (default 127.0.0.1; 0.0.0.0 on the hub)

Degrades honestly: a missing TLE file is a 503 with a message, never an empty
"no passes tonight". Passes are recomputed at most once a minute; the keps check
runs at most once every ten minutes because it fetches from the internet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import passes as engine

HERE = Path(__file__).resolve().parent
TLE = Path(os.environ.get("SHACKTRACK_TLE", HERE / "amateur.tle"))
SATS = Path(os.environ.get("SHACKTRACK_SATS", HERE / "satellites.json"))
KEPS = os.environ.get("SHACKTRACK_KEPS", str(HERE / "keps-watch.py"))
PORT = int(os.environ.get("SHACKTRACK_PORT", "8781"))
BIND = os.environ.get("SHACKTRACK_BIND", "127.0.0.1")

PASS_TTL_S = 60
KEPS_TTL_S = 600

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cached(key: str, ttl: float, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/passes")
def api_passes():
    try:
        hours = max(1.0, min(48.0, float(request.args.get("hours", 12))))
    except ValueError:
        return jsonify({"error": "hours must be a number"}), 400
    try:
        result = _cached(f"passes:{hours}", PASS_TTL_S,
                         lambda: engine.compute(engine.load_config(SATS), TLE, hours))
    except (OSError, ValueError) as e:
        # The case the page must SHOW. A missing TLE file is not "no passes".
        return jsonify({"error": f"pass engine cannot read its inputs: {e}",
                        "tle_file": str(TLE), "sats_file": str(SATS)}), 503
    except Exception as e:  # noqa: BLE001 -- a bug in the engine must still reach the page as JSON
        app.logger.exception("pass engine failed")
        return jsonify({"error": f"pass engine failed: {type(e).__name__}: {e}"}), 500
    return jsonify(result)


def _run_keps() -> dict:
    if not KEPS:
        return {"disabled": True}
    cmd = [sys.executable, KEPS, "--json"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"keps-watch did not run: {e}"}
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {"error": "keps-watch produced no JSON",
                "exit": cp.returncode, "stderr": cp.stderr[-400:]}
    data["fetched"] = _now_iso()
    return data


@app.get("/api/keps")
def api_keps():
    return jsonify(_cached("keps", KEPS_TTL_S, _run_keps))


@app.get("/api/health")
def api_health():
    info = {"now": _now_iso(), "tle_file": str(TLE), "sats_file": str(SATS),
            "tle_exists": TLE.exists(), "sats_exists": SATS.exists()}
    if TLE.exists():
        mtime = datetime.fromtimestamp(TLE.stat().st_mtime, tz=timezone.utc)
        info["tle_mtime"] = mtime.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return jsonify(info)


if __name__ == "__main__":
    print(f"shack-track: TLE={TLE} sats={SATS} keps={KEPS or '(off)'} on {BIND}:{PORT}",
          file=sys.stderr)
    app.run(host=BIND, port=PORT, debug=False, threaded=True)
