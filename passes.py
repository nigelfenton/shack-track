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

# de421 gives the sunlit flag (a linear bird in eclipse is often switched off).
# It is a download on first use; without it `sunlit` is simply null.
try:
    _EPH = load("de421.bsp")
except Exception:  # noqa: BLE001 -- offline, or no write access to the cache
    _EPH = None

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


EARTH_R_KM = 6371.0


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


def bird(sat, site, t, downlink_hz: float | None = None) -> dict:
    """The satellite's own state -- the fields Gpredict's window carries.

    Everything here is about the BIRD, not about us pointing at it: where it is
    over the Earth, how high and how fast, how much of the world can see it,
    and where it is in its orbit. Path loss and delay come from the slant range
    and so belong to the link, but they are what an operator reads next to it.
    """
    geo = sat.at(t)
    sp = wgs84.subpoint(geo)
    alt_km = float(sp.elevation.km)
    # Speed in the geocentric frame; velocity is in AU/day.
    vx, vy, vz = geo.velocity.km_per_s
    speed = float((vx * vx + vy * vy + vz * vz) ** 0.5)
    # Footprint: the great-circle radius of the visible cap, horizon at 0 deg.
    import math
    r = EARTH_R_KM + alt_km
    central = math.degrees(math.acos(min(1.0, EARTH_R_KM / r)))
    out = {
        "lat": round(float(sp.latitude.degrees), 3),
        "lon": round(float(sp.longitude.degrees), 3),
        "alt_km": round(alt_km, 1),
        "speed_kms": round(speed, 3),
        "footprint_km": round(2 * math.pi * EARTH_R_KM * central / 360.0, 0),
        "orbit": int(getattr(sat.model, "revnum", 0) or 0),
        "period_min": round(2 * math.pi / sat.model.no_kozai, 1) if getattr(sat.model, "no_kozai", 0) else None,
        "inclination_deg": round(math.degrees(sat.model.inclo), 2) if hasattr(sat.model, "inclo") else None,
        "eccentricity": round(float(sat.model.ecco), 6) if hasattr(sat.model, "ecco") else None,
        "grid": maidenhead(float(sp.latitude.degrees), float(sp.longitude.degrees)),
        "sunlit": bool(geo.is_sunlit(_EPH)) if _EPH is not None else None,
    }
    # Mean anomaly (0-360) says where in the orbit it is; Gpredict shows it
    # because the linear-transponder birds are often scheduled on it.
    if hasattr(sat.model, "mo"):
        mm = sat.model.no_kozai * 1440.0 / (2 * math.pi)   # rev/day
        days = t.tt - sat.epoch.tt
        ma = (math.degrees(sat.model.mo) + 360.0 * mm * days) % 360.0
        out["mean_anomaly"] = round(ma, 1)
        out["orbit"] = int((getattr(sat.model, "revnum", 0) or 0) + mm * days)
    # Link numbers: free-space path loss and one-way delay for the downlink.
    d = (sat - site).at(t)
    _, _, dist = d.altaz()
    rng = float(dist.km)
    out["slant_range_km"] = round(rng, 1)
    out["delay_ms"] = round(rng / 299.792458, 2)
    if downlink_hz:
        out["path_loss_db"] = round(
            20 * math.log10(rng * 1000.0) + 20 * math.log10(downlink_hz) - 147.55, 1)
    return out


def maidenhead(lat: float, lon: float) -> str:
    """6-character grid square, the way every ham names a position."""
    lon += 180.0
    lat += 90.0
    a = chr(ord("A") + int(lon // 20))
    b = chr(ord("A") + int(lat // 10))
    c = str(int((lon % 20) // 2))
    d = str(int(lat % 10))
    # Subsquares: 24 per field, so a longitude subsquare is 5 minutes of arc
    # (2 deg / 24) and a latitude one is 2.5 minutes (1 deg / 24). Getting the
    # longitude divisor wrong put II22TB one square east as II22ub.
    e = chr(ord("a") + int((lon % 2) / 2 * 24))
    f = chr(ord("a") + int((lat % 1) * 24))
    return a + b + c + d + e + f


def grid_to_latlon(locator: str) -> tuple[float, float]:
    """Centre of a 4- or 6-character Maidenhead square.

    The CENTRE, not the corner: a 6-character square is 5 minutes of longitude
    by 2.5 of latitude (about 4.6 x 4.6 km here), so taking the corner would
    put a station up to 3 km from where they said they were -- the same class
    of error that had this project's own QTH one subsquare east for months.

    Raises ValueError with a sentence the page can show the operator, rather
    than returning a silent default: a typo'd grid that quietly computes
    somewhere else is exactly the Copenhagen failure Gpredict shipped.
    """
    loc = (locator or "").strip().upper()
    if len(loc) not in (4, 6):
        raise ValueError("A grid square is 4 or 6 characters, like II22 or II22TB.")
    if not ("A" <= loc[0] <= "R" and "A" <= loc[1] <= "R"):
        raise ValueError("The first two letters of a grid run A to R.")
    if not (loc[2].isdigit() and loc[3].isdigit()):
        raise ValueError("Characters three and four of a grid are digits, like II22.")
    lon = (ord(loc[0]) - 65) * 20.0 - 180.0 + int(loc[2]) * 2.0
    lat = (ord(loc[1]) - 65) * 10.0 - 90.0 + int(loc[3])
    if len(loc) == 6:
        if not ("A" <= loc[4] <= "X" and "A" <= loc[5] <= "X"):
            raise ValueError("The last two letters of a 6-character grid run A to X.")
        # 24 subsquares per field in each axis; + half a subsquare for the centre.
        lon += (ord(loc[4]) - 65) * (2.0 / 24.0) + (1.0 / 24.0)
        lat += (ord(loc[5]) - 65) * (1.0 / 24.0) + (0.5 / 24.0)
    else:
        lon += 1.0   # centre of the 2-degree field
        lat += 0.5   # centre of the 1-degree field
    return lat, lon


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


# How close the radio must be to a bird's Doppler-corrected downlink before we
# believe it is listening to that bird. A linear transponder is ~60 kHz wide
# and the operator tunes inside it, so this must cover half a passband plus
# Doppler -- but no wider, or it starts claiming birds that merely share a
# band. 145.900 is 45 kHz from JO-97 and is NOT JO-97; a 120 kHz window said
# it was.
FOLLOW_WINDOW_HZ = 40_000


def follow_radio(passes: list[dict], radio_hz: int | None, now_iso: str,
                 range_rates: dict | None = None) -> dict | None:
    """Which listed bird is the radio actually listening to, if any?

    The view used to pick the earliest live pass, which on 2026-09-02 meant it
    drew AO-123, then AO-27, while the radio was on RS-44 the whole time --
    three wrong birds in one evening. The radio's own frequency is better
    evidence of the operator's intent than the clock is.

    Only passes that are ACTUALLY UP are candidates: matching the frequency of
    a bird still below the horizon is a coincidence, not a choice, and would
    have the view following something the operator cannot hear.
    """
    if not radio_hz:
        return None
    best, best_delta = None, None
    for p in passes:
        dn_khz = p.get("downlink_khz")
        if not dn_khz:
            continue
        if not (p["aos"] <= now_iso < p["los"]):
            continue
        # Compare against the Doppler-corrected downlink where we know the
        # range rate, else the rest frequency; the window is wide enough that
        # a few kHz of Doppler cannot change the answer.
        centre = dn_khz * 1000
        rr = (range_rates or {}).get(p["sat"])
        if rr is not None:
            d = doppler(centre, rr, uplink=False)
            if d:
                centre = d["hz"]
        delta = abs(radio_hz - centre)
        if delta <= FOLLOW_WINDOW_HZ and (best_delta is None or delta < best_delta):
            best, best_delta = p, delta
    return best


def live_state(cfg: dict, tle_path: Path, now: datetime | None = None,
               precomputed: dict | None = None, sat: str | None = None,
               radio_hz: int | None = None) -> dict:
    """What the operating view needs, once a second.

    Picks the pass in progress (else the next one within 24 h), and reports the
    bird's current geometry and Doppler-corrected frequencies for it. Also
    reports `az_el_now` for the next bird even while it is below the horizon,
    so the view can show where it will rise.
    """
    ts = load.timescale()
    now = now or datetime.now(timezone.utc)
    t = ts.from_datetime(now)
    # `precomputed` is the server's cached 24 h pass list. Without it every
    # 1 Hz poll recomputed 14 birds x 24 h (seconds on the hub) and the live
    # endpoint timed out under its own polling on 2026-09-02.
    result = precomputed or compute(cfg, tle_path, 24.0, now=now)
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    # `sat` pins the view to one bird (the operator's choice, e.g. what Gpredict
    # is tracking) even while another bird's pass overlaps. Without it: the
    # earliest live pass, else the next one. Passes overlap more than you'd
    # think -- AO-27 and RS-44 did on 2026-09-02.
    cands = [p for p in result["passes"] if not sat or p["sat"].lower() == sat.lower()]

    # Selection, in order of how good the evidence is:
    #   1. an explicit ?sat= pin -- the operator said so
    #   2. the bird the RADIO is listening to -- the operator did so
    #   3. the schedule -- a guess, and the one that was wrong all evening
    chosen, how = None, "schedule"
    if sat:
        how = "sat"
    elif radio_hz:
        chosen = follow_radio(cands, radio_hz, now_iso)
        if chosen is not None:
            how = "radio"

    if chosen is None:
        chosen = next((p for p in cands if p["aos"] <= now_iso < p["los"]), None)
        if chosen is None:
            chosen = next((p for p in cands if p["aos"] > now_iso), None)
    state = "none" if chosen is None else ("live" if chosen["aos"] <= now_iso < chosen["los"] else "next")

    out = {"now": now_iso, "state": state, "pass": chosen, "selected_by": how,
           "min_elevation_deg": result["min_elevation_deg"],
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
        "bird": bird(sat, site, t, downlink_hz=dn or None),
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
                "source": s.get("source", ""),
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
