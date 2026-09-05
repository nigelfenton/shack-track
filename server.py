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

import csv
from io import StringIO
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
from tracker import Tracker

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

# HOW STALE IS TOO STALE TO SERVE AT ALL.
#
# The TTLs above decide when to REFRESH. These decide when a stale value stops
# being worth showing, and the caller waits for a real one instead. Without a
# ceiling, stale-while-revalidate serves whatever was last computed no matter
# how old: this page was observed handing back a pass list from 14:46Z at
# 22:03Z -- seven hours out, with /api/live picking a finished pass as
# "current" while /api/next disagreed with it.
#
# That happens because nothing polls this service. It was written for a 1 Hz
# viewer, but the real access pattern is nobody for hours and then one person
# opening the page just before a pass -- so the FIRST request, the one that
# matters most, is the one guaranteed to be stale. Only the second is right.
#
# Serving inline is affordable: a cold 24 h recompute measures 2.58 s on the
# hub (2.59/2.57/2.59 over three runs; 1 h = 0.37 s, 6 h = 1.23 s). The old
# docstring's "~20 s" is what justified unbounded staleness, and it is wrong
# by a factor of eight. Measure before inheriting a constraint.
PASS_MAX_STALE_S = 300     # five minutes: still the right passes, wrong countdown
LIVE_MAX_STALE_S = 5       # a live view showing 5 s old geometry is lying
RIGCTL_TIMEOUT_S = 0.7

# AetherSDR IS a rigctl server (port 4532) -- no Hamlib. aurora13's LAN address
# is DHCP-assigned and has moved before; the Tailscale address is stable.
RIGCTL = os.environ.get("SHACKTRACK_RIGCTL", "10.0.0.104:4532")

# Where the standing engagement is remembered across a restart. An unattended
# overnight capture must not end because the hub rebooted.
TRACK_STATE = Path(os.environ.get("SHACKTRACK_STATE", HERE / "tracker-state.json"))

# Set SHACKTRACK_TRACK=0 to serve the pages read-only, with no ability to tune
# the radio at all -- what a public deployment would want.
TRACK_ENABLED = os.environ.get("SHACKTRACK_TRACK", "1") not in ("0", "false", "no")

app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_refreshing: set[str] = set()


def _cached(key: str, ttl: float, fn, *, max_stale: float | None = None):
    """Cache with stale-while-revalidate and a staleness CEILING.

    Inside `ttl` the cached value is served directly. Past it the value is
    still served -- but only while it is younger than `max_stale`, and a
    background refresh is kicked off so the next caller gets a fresh one. Past
    `max_stale` the entry is treated as absent and recomputed inline, because
    a pass list from seven hours ago is not a slightly-late answer, it is a
    wrong one.

    `max_stale=None` keeps the old unbounded behaviour, which is right only
    for values whose staleness is self-evident to the caller.

    A background refresh that RAISES evicts the entry rather than leaving it to
    be served forever: the previous version logged the exception and kept
    handing out the stale value with no ceiling and no further attempt to
    replace it, so one transient failure became permanent bad data.
    """
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit:
            age = now - hit[0]
            if age < ttl:
                return hit[1]
            # Expired. Serve it only if it is still inside the ceiling.
            servable = max_stale is None or age < max_stale
            if key not in _refreshing:
                _refreshing.add(key)

                def refresh():
                    try:
                        value = fn()
                        with _lock:
                            _cache[key] = (time.monotonic(), value)
                    except Exception:  # noqa: BLE001
                        app.logger.exception("background refresh of %s failed", key)
                        with _lock:
                            _cache.pop(key, None)
                    finally:
                        with _lock:
                            _refreshing.discard(key)

                threading.Thread(target=refresh, name=f"refresh-{key}", daemon=True).start()
            if servable:
                return hit[1]
            # Too stale to show. Fall through and compute inline; the caller
            # waits ~2.6 s at worst rather than being told yesterday's news.
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
                         lambda: engine.compute(engine.load_config(SATS), TLE, hours),
                         max_stale=PASS_MAX_STALE_S)
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


# How many distinct grids we will hold pass lists for at once. Each entry is a
# 24 h list (~280 kB) and costs ~2.6 s to build, so this is the knob that stops
# a public URL turning into an unbounded compute-and-memory sink: ?grid= is the
# one parameter a stranger controls, and without a cap they could mint a new
# cache key on every request.
GRID_CACHE_MAX = 24
_grid_seen: list[str] = []


def _grid_cfg(grid: str) -> dict:
    """The station config with someone else's QTH substituted.

    A shallow copy with a fresh qth dict: the satellite table, minimum
    elevation and everything else stay shared, but nothing this request does
    can reach back and change the operator's own configured position.
    """
    cfg = dict(engine.load_config(SATS))
    lat, lon = engine.grid_to_latlon(grid)      # raises ValueError on a bad grid
    cfg["qth"] = {"name": grid.upper(), "lat": round(lat, 4), "lon": round(lon, 4),
                  "alt_m": 0}
    return cfg


def _grid_key(grid: str) -> str:
    """Cache key for a visitor's grid, with a bound on how many we keep."""
    key = "passes:grid:" + grid.upper()
    with _lock:
        if key in _grid_seen:
            _grid_seen.remove(key)
        _grid_seen.append(key)
        while len(_grid_seen) > GRID_CACHE_MAX:
            _cache.pop(_grid_seen.pop(0), None)
    return key


@app.get("/api/passes/grid")
def api_passes_grid():
    """Passes for ANY Maidenhead grid -- the public predictor.

    Deliberately needs no login and reads nothing about this station: it is
    ephemeris for a location the caller supplied, computable by anyone holding
    the same TLEs. That is why it can be public when /api/live cannot.

    The grid is validated before it reaches the engine and a bad one comes back
    as a sentence to show the operator, not a default position -- a typo that
    silently computes somewhere else is the Copenhagen failure.
    """
    grid = (request.args.get("grid") or "").strip()
    if not grid:
        return jsonify({"error": "Give a grid square, like ?grid=II22TB"}), 400
    try:
        hours = max(1.0, min(48.0, float(request.args.get("hours", 24))))
    except ValueError:
        return jsonify({"error": "hours must be a number"}), 400
    try:
        cfg = _grid_cfg(grid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        key = _grid_key(grid) + ":%s" % hours
        result = _cached(key, PASS_TTL_S,
                         lambda: engine.compute(cfg, TLE, hours),
                         max_stale=PASS_MAX_STALE_S)
    except (OSError, ValueError) as e:
        return jsonify({"error": "pass engine cannot read its inputs: %s" % e}), 503
    except Exception as e:  # noqa: BLE001
        app.logger.exception("pass engine failed for grid %s", grid)
        return jsonify({"error": "pass engine failed: %s" % type(e).__name__}), 500
    return jsonify(result)


def _export_passes():
    """The 24 h list every export shares. Always 24 h regardless of the page's
    view toggle -- a file called 'passes' that silently held one hour because a
    button was set that way is a worse surprise than one that holds a day."""
    cfg = engine.load_config(SATS)
    return _cached("passes:24.0", PASS_TTL_S,
                   lambda: engine.compute(cfg, TLE, 24.0),
                   max_stale=PASS_MAX_STALE_S)


def _attach(body: str, mime: str, name: str):
    """A downloadable response. Content-Disposition is what makes the browser
    save it rather than render it; without it a CSV shows as a wall of text."""
    return app.response_class(
        body, mimetype=mime,
        headers={"Content-Disposition": 'attachment; filename="%s"' % name,
                 "Cache-Control": "no-store"})


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


@app.get("/api/export/passes.csv")
def export_csv():
    """The pass list as a spreadsheet. UTC and local side by side: the times you
    work a pass by are local, but the times you compare against anyone else's
    log are UTC, and a file with only one of them always turns out to be the
    wrong one."""
    try:
        result = _export_passes()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "pass engine failed: %s" % e}), 500
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["satellite", "mode", "aos_utc", "aos_local", "tca_utc", "los_utc",
                "duration_min", "peak_el_deg", "aos_az_deg", "peak_az_deg",
                "los_az_deg", "uplink_khz", "downlink_khz", "transponder", "note"])
    for p_ in result["passes"]:
        aos = p_.get("aos", "")
        try:
            local = (datetime.fromisoformat(aos.replace("Z", "+00:00"))
                     .astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
        except (ValueError, AttributeError):
            local = ""
        w.writerow([
            p_.get("display", ""), p_.get("mode", ""), aos, local,
            p_.get("tca", ""), p_.get("los", ""),
            round((p_.get("duration_s") or 0) / 60.0, 1),
            p_.get("peak_el", ""), p_.get("aos_az", ""), p_.get("peak_az", ""),
            p_.get("los_az", ""), p_.get("uplink_khz", ""), p_.get("downlink_khz", ""),
            p_.get("xpdr", ""), p_.get("note", ""),
        ])
    return _attach(buf.getvalue(), "text/csv",
                   "shack-track-passes-%s.csv" % _stamp())


@app.get("/api/export/passes.ics")
def export_ics():
    """Passes as calendar events.

    RFC 5545 wants CRLF line endings and escaped commas/semicolons in TEXT
    values -- a raw note containing a comma silently truncates the field in
    some clients. UID must be stable per pass so re-importing updates an event
    rather than duplicating it.
    """
    try:
        result = _export_passes()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "pass engine failed: %s" % e}), 500

    def esc(t):
        return (str(t).replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))

    def stamp(iso):
        return str(iso).replace("-", "").replace(":", "").replace(".000", "")

    out = ["BEGIN:VCALENDAR", "VERSION:2.0",
           "PRODID:-//G0JKN//Shack-Track//EN", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:Satellite passes (%s)"
           % result.get("qth", {}).get("name", "")]
    now = stamp(_now_iso())
    for p_ in result["passes"]:
        summary = "%s  %s deg" % (p_.get("display", "pass"), p_.get("peak_el", "?"))
        desc = ("Peak elevation %s deg at azimuth %s. AOS az %s, LOS az %s. "
                "Downlink %s kHz, uplink %s kHz. %s"
                % (p_.get("peak_el", "?"), p_.get("peak_az", "?"),
                   p_.get("aos_az", "?"), p_.get("los_az", "?"),
                   p_.get("downlink_khz", "?"), p_.get("uplink_khz", "?"),
                   p_.get("note", "")))
        out += ["BEGIN:VEVENT",
                "UID:%s-%s@track.g0jkn.com" % (p_.get("display", "pass"),
                                               stamp(p_.get("aos", ""))),
                "DTSTAMP:" + now,
                "DTSTART:" + stamp(p_.get("aos", "")),
                "DTEND:" + stamp(p_.get("los", "")),
                "SUMMARY:" + esc(summary),
                "DESCRIPTION:" + esc(desc),
                "END:VEVENT"]
    out.append("END:VCALENDAR")
    return _attach("\r\n".join(out) + "\r\n", "text/calendar",
                   "shack-track-passes-%s.ics" % _stamp())


@app.get("/api/export/passes.json")
def export_json():
    """Exactly what /api/passes returns for 24 h, as a saved file."""
    try:
        result = _export_passes()
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "pass engine failed: %s" % e}), 500
    return _attach(json.dumps(result, indent=2), "application/json; charset=utf-8",
                   "shack-track-passes-%s.json" % _stamp())


@app.get("/api/export/satellites.json")
def export_sats():
    """The bird table itself -- frequencies, modes, transponder sense and the
    provenance note on each entry. Served from disk rather than the parsed
    object so the _comment block and key order survive for a human reader."""
    try:
        return _attach(Path(SATS).read_text(encoding="utf-8"),
                       "application/json; charset=utf-8",
                       "shack-track-satellites.json")
    except OSError as e:
        return jsonify({"error": "cannot read satellite table: %s" % e}), 500


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
                         lambda: engine.compute(engine.load_config(SATS), TLE, 24.0),
                         max_stale=PASS_MAX_STALE_S)
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
        sat = (request.args.get("sat") or "").strip() or None
        passes24 = _cached("passes:24.0", PASS_TTL_S,
                           lambda: engine.compute(cfg, TLE, 24.0),
                           max_stale=PASS_MAX_STALE_S)
        # Read the radio FIRST when no pin is set, so the view can follow the
        # bird the operator is actually listening to rather than the clock.
        radio_first = _cached("radio", RADIO_TTL_S, _read_radio) if not sat else None
        rhz = radio_first.get("hz") if (radio_first and radio_first.get("reachable")) else None
        state = _cached(f"live:{sat or '*'}:{(rhz or 0) // 100000}", LIVE_TTL_S,
                        lambda: engine.live_state(cfg, TLE, precomputed=passes24, sat=sat,
                                                  radio_hz=rhz),
                        max_stale=LIVE_MAX_STALE_S)
    except (OSError, ValueError) as e:
        return jsonify({"error": f"pass engine cannot read its inputs: {e}"}), 503
    except Exception as e:  # noqa: BLE001
        app.logger.exception("live state failed")
        return jsonify({"error": f"live state failed: {type(e).__name__}: {e}"}), 500
    radio = _cached("radio", RADIO_TTL_S, _read_radio)
    out = dict(state)
    out["radio"] = radio
    out["rotor"] = {"present": False}
    tr = tracker.status()
    tr["enabled"] = TRACK_ENABLED
    out["tracker"] = tr
    return jsonify(out)


def _live_for(sat: str) -> dict:
    """Live state for one bird, from the same cache the operating view reads."""
    try:
        cfg = engine.load_config(SATS)
        passes24 = _cached("passes:24.0", PASS_TTL_S,
                           lambda: engine.compute(cfg, TLE, 24.0),
                           max_stale=PASS_MAX_STALE_S)
        return _cached(f"live:{sat}:pinned", LIVE_TTL_S,
                       lambda: engine.live_state(cfg, TLE, precomputed=passes24, sat=sat),
                       max_stale=LIVE_MAX_STALE_S)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


tracker = Tracker(TRACK_STATE, _live_for, _rigctl)


@app.get("/api/track")
def api_track_status():
    st = tracker.status()
    st["enabled"] = TRACK_ENABLED
    st["log"] = tracker.log()[-60:]
    return jsonify(st)


@app.post("/api/track")
def api_track_set():
    """Engage or disengage. Engaging is a STANDING instruction: it survives LOS,
    a server restart and the radio going away, until it is turned off."""
    if not TRACK_ENABLED:
        return jsonify({"ok": False, "error": "tracking is disabled on this server"}), 403
    body = request.get_json(silent=True) or {}
    if body.get("engage"):
        sat = (body.get("sat") or "").strip()
        if not sat:
            return jsonify({"ok": False, "error": "sat is required"}), 400
        out = tracker.engage(sat, offset=int(body.get("offset", 0)))
        return jsonify(out), (200 if out.get("ok") else 409)
    return jsonify(tracker.disengage())


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
    # Warm the 24 h list so the first live poll after a restart does not wait on
    # the recompute (measured 2.58 s cold on the hub, not the ~20 s once claimed).
    def warm():
        # WARM WHAT THE PAGE ACTUALLY ASKS FOR. The 24 h list backs /api/live and
        # /api/next, but static/index.html requests `/api/passes?hours=12`, which
        # is a DIFFERENT cache key -- so priming only 24.0 left the first visitor
        # computing 12.0 from cold while this thread held the CPU for the other.
        # Measured on one restart: the 24 h list answered in 0.04 s while the
        # 12 h request the page makes took 18.7 s. Both now, in the page's order.
        try:
            cfg = engine.load_config(SATS)
            for hours in (12.0, 24.0):
                _cached("passes:%s" % hours, PASS_TTL_S,
                        lambda h=hours: engine.compute(cfg, TLE, h),
                        max_stale=PASS_MAX_STALE_S)
            print("shack-track: pass cache warm", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print("shack-track: warm-up failed: %s" % e, file=sys.stderr)
    threading.Thread(target=warm, name="warm", daemon=True).start()
    # Re-engage whatever was engaged before the restart, once the cache is warm
    # enough to answer. This is what makes an overnight run survive a reboot.
    def resume():
        time.sleep(3)
        if TRACK_ENABLED:
            tracker.load()
    threading.Thread(target=resume, name="resume-track", daemon=True).start()
    app.run(host=BIND, port=PORT, debug=False, threaded=True)
