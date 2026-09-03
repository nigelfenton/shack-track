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
import socket
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
LIVE_TTL_S = 0.9       # the view polls at 1 Hz; never serve two callers two computes
RADIO_TTL_S = 0.9
RIGCTL_TIMEOUT_S = 0.7

# AetherSDR IS a rigctl server (port 4532) -- no Hamlib. aurora13's LAN address
# is DHCP-assigned and has moved before; the Tailscale address is stable.
RIGCTL = os.environ.get("SHACKTRACK_RIGCTL", "10.0.0.104:4532")

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_refreshing: set[str] = set()


def _cached(key: str, ttl: float, fn):
    """Cache with stale-while-revalidate. An expired entry is returned as-is
    and refreshed on a background thread, so a 1 Hz poller never waits on a
    recompute (the 24 h pass list takes ~20 s on the hub). Only a cold cache
    computes inline."""
    with _lock:
        hit = _cache.get(key)
        fresh = hit and time.monotonic() - hit[0] < ttl
        if hit and not fresh and key not in _refreshing:
            _refreshing.add(key)
            def refresh():
                try:
                    value = fn()
                    with _lock:
                        _cache[key] = (time.monotonic(), value)
                except Exception:  # noqa: BLE001
                    app.logger.exception("background refresh of %s failed", key)
                finally:
                    with _lock:
                        _refreshing.discard(key)
            threading.Thread(target=refresh, name=f"refresh-{key}", daemon=True).start()
        if hit:
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


@app.get("/strip")
def strip():
    return send_from_directory(app.static_folder, "strip.html")


@app.get("/api/next")
def api_next():
    """The one pass the status strip (and anything else small) needs.

    `live` is the pass in progress right now, if any; `next` is the first pass
    whose AOS is still ahead. Both are full pass records from /api/passes over
    the next 24 h, so a consumer gets az track and frequencies without a second
    call. Either can be null; both null means nothing above the horizon for a
    day, which is a real answer and not an error.
    """
    try:
        result = _cached("passes:24.0", PASS_TTL_S,
                         lambda: engine.compute(engine.load_config(SATS), TLE, 24.0))
    except (OSError, ValueError) as e:
        return jsonify({"error": f"pass engine cannot read its inputs: {e}"}), 503
    except Exception as e:  # noqa: BLE001
        app.logger.exception("pass engine failed")
        return jsonify({"error": f"pass engine failed: {type(e).__name__}: {e}"}), 500
    now = _now_iso()
    live = next((p for p in result["passes"] if p["aos"] <= now < p["los"]), None)
    upcoming = next((p for p in result["passes"] if p["aos"] > now), None)
    return jsonify({"now": now, "live": live, "next": upcoming,
                    "tle_newest_epoch": result["tle_newest_epoch"],
                    "qth": result["qth"]})


@app.get("/live")
def live_page():
    return send_from_directory(app.static_folder, "live.html")


def _rigctl(cmd: bytes) -> str | None:
    """One rigctl round trip. RECEIVE ONLY: only 'f' and 'm' are ever sent.
    None means unreachable, and the caller must SAY so rather than reuse a
    stale number -- a frozen frequency presented as current is the failure
    the 2026-08-29 ISS log was made of."""
    if not RIGCTL:
        return None
    host, _, port = RIGCTL.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=RIGCTL_TIMEOUT_S) as s:
            s.sendall(cmd + b"\n")
            return s.recv(200).decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return None


def _read_radio() -> dict:
    f = _rigctl(b"f")
    if f is None:
        return {"reachable": False, "target": RIGCTL or "(no rigctl configured)"}
    m = _rigctl(b"m") or ""
    try:
        hz = int(f.split()[0])
    except (ValueError, IndexError):
        return {"reachable": False, "target": RIGCTL, "error": f"unexpected reply to f: {f!r}"}
    mode = m.split("\n")[0] if m else ""
    return {"reachable": True, "target": RIGCTL, "hz": hz, "mode": mode,
            "read_at": _now_iso()}


@app.get("/api/live")
def api_live():
    """Once-a-second truth for the operating view: bird geometry + Doppler from
    Skyfield, and what the radio is ACTUALLY on from rigctl. The two are
    reported side by side and never merged -- the point of the view is to
    show when they disagree."""
    try:
        cfg = engine.load_config(SATS)
        passes24 = _cached("passes:24.0", PASS_TTL_S, lambda: engine.compute(cfg, TLE, 24.0))
        state = _cached("live", LIVE_TTL_S,
                        lambda: engine.live_state(cfg, TLE, precomputed=passes24))
    except (OSError, ValueError) as e:
        return jsonify({"error": f"pass engine cannot read its inputs: {e}"}), 503
    except Exception as e:  # noqa: BLE001
        app.logger.exception("live state failed")
        return jsonify({"error": f"live state failed: {type(e).__name__}: {e}"}), 500
    radio = _cached("radio", RADIO_TTL_S, _read_radio)
    out = dict(state)
    out["radio"] = radio
    out["rotor"] = {"present": False}
    return jsonify(out)


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
    # Warm the 24 h list so the first live poll after a restart is not a 20 s wait.
    def warm():
        try:
            _cached("passes:24.0", PASS_TTL_S, lambda: engine.compute(engine.load_config(SATS), TLE, 24.0))
            print("shack-track: pass cache warm", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"shack-track: warm-up failed: {e}", file=sys.stderr)
    threading.Thread(target=warm, name="warm", daemon=True).start()
    app.run(host=BIND, port=PORT, debug=False, threaded=True)
