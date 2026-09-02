#!/usr/bin/env python3
"""passes.py -- Shack-Track pass engine.

Given a QTH and a satellite list, return the next passes as plain data. This is
weekend-one item 1 from WEEKEND-ONE.md: a refactor of what render-fd-sats.py
already does on shack-hub, with the Field Day specifics (fixed window, Grav page,
polar SVG) stripped out so a server can call it.

The orbital maths is Skyfield's, not ours. Validated 2026-08-28: AOS within 14 s of
SatPC32's independent SGP4 for the same TLE.

    python passes.py                       # next 12 h, human table
    python passes.py --hours 24 --json     # JSON, what /api/passes returns
    python passes.py --tle /home/nigel/satpass/amateur.tle

Exit status is 0 even when no passes fall in the window; it is 2 if the TLE file or
the satellite list cannot be read, which is the case the caller must show rather
than render as "no passes tonight".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

HERE = Path(__file__).resolve().parent
DEFAULT_SATS = HERE / "satellites.json"
DEFAULT_TLE = HERE / "amateur.tle"

# Elevation classes for the pass list. The mockup's judgement is "worth staying
# up for": an overhead pass and a scrape must look different at a glance.
EL_CLASS = (("hi", 45.0), ("mid", 20.0), ("lo", 0.0))


def el_class(peak_el: float) -> str:
    for name, floor in EL_CLASS:
        if peak_el >= floor:
            return name
    return "lo"


# --- inputs -----------------------------------------------------------------

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for k in ("qth", "satellites"):
        if k not in cfg:
            raise ValueError(f"{path}: missing '{k}'")
    return cfg


def parse_tles(path: Path, ts):
    """Return {name: EarthSatellite} for every well-formed 3-line entry."""
    sats = {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    i = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i].strip(), lines[i + 1].strip(), lines[i + 2].strip()
        if l1.startswith("1 ") and l2.startswith("2 "):
            try:
                sats[name] = EarthSatellite(l1, l2, name, ts)
            except Exception:
                pass
            i += 3
        else:
            i += 1
    return sats


def match_tle(prefix: str, tles: dict):
    p = prefix.lower()
    for name, sat in tles.items():
        if name.lower().startswith(p):
            return name, sat
    return None, None


# --- geometry ---------------------------------------------------------------

def _altaz(diff, t):
    alt, az, _ = diff.at(t).altaz()
    return float(alt.degrees), float(az.degrees)


def _iso(t) -> str:
    return t.utc_datetime().replace(microsecond=0).isoformat().replace("+00:00", "Z")


# How far before the window start to search, so a pass already in progress at
# t0 is reported with the AOS it really had rather than "when we looked".
# Nothing in the amateur list stays up longer than IO-117's ~70 min at MEO;
# LEO passes are under 20 min. 90 min covers both with margin.
LOOKBACK_MIN = 90


def passes_for(sat, site, ts, t0, t1, min_el: float) -> list[dict]:
    """Every pass of `sat` over `site` still up at or after t0, ending by t1.

    The search starts LOOKBACK_MIN before t0 so a pass in progress at t0 keeps
    its true AOS and is flagged `in_progress`; passes that ended before t0 are
    dropped. A pass still up at t1 is reported with LOS = t1 and `clipped`.
    """
    diff = sat - site
    out = []
    t_search = ts.tt_jd(t0.tt - LOOKBACK_MIN / 1440.0)
    times, events = sat.find_events(site, t_search, t1, altitude_degrees=min_el)

    aos = culm = None
    el_start, _ = _altaz(diff, t_search)
    if el_start >= min_el:
        aos = t_search                    # up before the lookback even began

    for t, ev in zip(times, events):
        if ev == 0:                       # rise
            aos, culm = t, None
        elif ev == 1:                     # culminate
            if aos is not None:
                culm = t
        elif ev == 2:                     # set
            if aos is None:
                continue
            if t.tt > t0.tt:              # ended before the window: not a pass to show
                peak = aos if culm is None else culm
                out.append(_describe(sat, diff, ts, aos, peak, t, in_progress=aos.tt < t0.tt))
            aos = culm = None

    # Pass still up at the end of the window: report it with LOS = t1, clipped.
    # NOT `culm or aos`: a Skyfield Time has no truth value (its __len__ raises
    # "this is a single Time, not an array"), which took the 6 h view down on
    # 2026-09-02 the first time anyone clicked it.
    if aos is not None:
        peak = aos if culm is None else culm
        out.append(_describe(sat, diff, ts, aos, peak, t1, in_progress=aos.tt < t0.tt, clipped=True))
    return out


def _describe(sat, diff, ts, aos, culm, los, in_progress, clipped=False) -> dict:
    peak_el, peak_az = _altaz(diff, culm)
    _, aos_az = _altaz(diff, aos)
    _, los_az = _altaz(diff, los)
    dur_s = int(round((los.tt - aos.tt) * 86400))
    n = max(8, min(120, dur_s // 10))
    tt = ts.tt_jd(np.linspace(aos.tt, los.tt, n))
    alts, azs, _ = diff.at(tt).altaz()
    track = [(round(float(a), 1), round(float(e), 1))
             for a, e in zip(azs.degrees, alts.degrees) if e >= 0]
    return {
        "aos": _iso(aos), "tca": _iso(culm), "los": _iso(los),
        "duration_s": dur_s,
        "peak_el": round(peak_el, 1), "peak_az": round(peak_az, 1),
        "aos_az": round(aos_az, 1), "los_az": round(los_az, 1),
        "el_class": el_class(peak_el),
        # bool(): the comparisons upstream are on NumPy floats and yield
        # numpy.bool_, which json/Flask refuse to serialise (500 on the hub,
        # 2026-09-02). Coerce at the boundary so callers never see it.
        "in_progress": bool(in_progress),
        "clipped": bool(clipped),
        "track": track,
    }


# --- live geometry and Doppler ------------------------------------------------

C_KM_S = 299_792.458


def geometry(sat, site, t) -> dict:
    """Where the bird is NOW from `site`: az/el, range and range rate.

    Range rate is what Doppler is made of. Negative = approaching (downlink
    heard HIGH, uplink must be sent LOW); it crosses zero at TCA.
    """
    d = (sat - site).at(t)
    alt, az, dist = d.altaz()
    _, _, _, _, _, rr = d.frame_latlon_and_rates(site)
    return {
        "az": round(float(az.degrees), 2), "el": round(float(alt.degrees), 2),
        "range_km": round(float(dist.km), 1),
        "range_rate_kms": round(float(rr.km_per_s), 4),
    }


def doppler(f_hz: float | None, range_rate_kms: float, uplink: bool) -> dict | None:
    """Frequency the radio should be on for f_hz to arrive at/leave the bird.

    Downlink: we RECEIVE, so f_obs = f_rest * (1 - rr/c).
    Uplink:   we TRANSMIT so it ARRIVES on f_rest: f_tx = f_rest * (1 + rr/c).
    Same convention as sat_capture.py and the 2026-08-29 RS-44 capture, where
    a radio driven by Gpredict was seen to follow the downlink value.
    """
    if not f_hz:
        return None
    sign = 1.0 if uplink else -1.0
    f = f_hz * (1.0 + sign * range_rate_kms / C_KM_S)
    return {"rest_hz": int(f_hz), "hz": int(round(f)), "shift_hz": int(round(f - f_hz))}


def live_state(cfg: dict, tle_path: Path, now: datetime | None = None) -> dict:
    """What the operating view needs, once a second.

    Picks the pass in progress (else the next one within 24 h), and reports the
    bird's current geometry and Doppler-corrected frequencies for it. Also
    reports `az_el_now` for the next bird even while it is below the horizon,
    so the view can show where it will rise.
    """
    ts = load.timescale()
    now = now or datetime.now(timezone.utc)
    t = ts.from_datetime(now)
    result = compute(cfg, tle_path, 24.0, now=now)
    now_iso = result["generated"]
    chosen = next((p for p in result["passes"] if p["aos"] <= now_iso < p["los"]), None)
    state = "live" if chosen else "next"
    if chosen is None:
        chosen = next((p for p in result["passes"] if p["aos"] > now_iso), None)
        state = "next" if chosen else "none"

    out = {"now": now_iso, "state": state, "pass": chosen,
           "tle_newest_epoch": result["tle_newest_epoch"], "qth": result["qth"]}
    if chosen is None:
        return out

    tles = parse_tles(tle_path, ts)
    _, sat = match_tle(chosen["tle_name"], tles)
    q = cfg["qth"]
    site = wgs84.latlon(q["lat"], q["lon"], elevation_m=q.get("alt_m", 0))
    g = geometry(sat, site, t)
    up = (chosen.get("uplink_khz") or 0) * 1000
    dn = (chosen.get("downlink_khz") or 0) * 1000
    out.update({
        "geometry": g,
        "uplink": doppler(up, g["range_rate_kms"], uplink=True),
        "downlink": doppler(dn, g["range_rate_kms"], uplink=False),
        "seconds_to_aos": int(round((datetime.fromisoformat(chosen["aos"].replace("Z", "+00:00")) - now).total_seconds())),
        "seconds_to_los": int(round((datetime.fromisoformat(chosen["los"].replace("Z", "+00:00")) - now).total_seconds())),
    })
    return out


# --- top level ----------------------------------------------------------------

def compute(cfg: dict, tle_path: Path, hours: float, min_el: float | None = None,
            now: datetime | None = None) -> dict:
    ts = load.timescale()
    now = now or datetime.now(timezone.utc)
    t0 = ts.from_datetime(now)
    t1 = ts.from_datetime(now + timedelta(hours=hours))
    min_el = cfg.get("min_elevation_deg", 5.0) if min_el is None else min_el

    q = cfg["qth"]
    site = wgs84.latlon(q["lat"], q["lon"], elevation_m=q.get("alt_m", 0))
    tles = parse_tles(tle_path, ts)

    passes, missing, epochs = [], [], {}
    for s in cfg["satellites"]:
        name, sat = match_tle(s["tle"], tles)
        if sat is None:
            missing.append(s["key"])
            continue
        epochs[s["key"]] = _iso(sat.epoch)
        for p in passes_for(sat, site, ts, t0, t1, min_el):
            p.update({
                "sat": s["key"], "display": s.get("display", s["key"]),
                "tle_name": name,
                "mode": s.get("mode", ""),
                "uplink_khz": s.get("uplink_khz"), "downlink_khz": s.get("downlink_khz"),
                "xpdr": s.get("xpdr"), "note": s.get("note", ""),
            })
            passes.append(p)
    passes.sort(key=lambda p: p["aos"])

    newest = max(epochs.values()) if epochs else None
    return {
        "generated": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "window_hours": hours, "min_elevation_deg": min_el,
        "qth": q, "tle_file": str(tle_path), "tle_count": len(tles),
        "tle_newest_epoch": newest,
        "missing_from_tle": missing,
        "passes": passes,
    }


def _table(result: dict) -> str:
    # ASCII only: this prints on a cp1252 Windows console as well as the hub.
    rows = [f"{'sat':8} {'AOS (UTC)':17} {'dur':>6} {'max el':>7} {'az aos>tca>los':>14}  mode"]
    for p in result["passes"]:
        aos = p["aos"][11:16]
        flag = "*" if p["in_progress"] else " "
        rows.append(f"{p['display']:8} {p['aos'][:10]} {aos}{flag} {p['duration_s']//60:>4}m "
                    f"{p['peak_el']:>6.0f}  {p['aos_az']:>4.0f}>{p['peak_az']:>3.0f}>{p['los_az']:>3.0f}  {p['mode']}")
    if result["missing_from_tle"]:
        rows.append(f"not in TLE: {', '.join(result['missing_from_tle'])}")
    rows.append(f"{len(result['passes'])} passes, {result['tle_count']} TLEs, newest epoch {result['tle_newest_epoch']}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sats", type=Path, default=DEFAULT_SATS)
    ap.add_argument("--tle", type=Path, default=DEFAULT_TLE)
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--min-el", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.sats)
        result = compute(cfg, a.tle, a.hours, a.min_el)
    except (OSError, ValueError) as e:
        print(f"passes: {e}", file=sys.stderr)
        return 2
    if a.json:
        json.dump(result, sys.stdout, indent=1)
        print()
    else:
        print(_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
