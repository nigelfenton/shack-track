#!/usr/bin/env python3
"""geo.py -- geostationary weather satellites: where to point, not when to listen.

The pass engine (passes.py) is built around rise and set. A geostationary bird
has neither: from a given QTH it is either parked in the sky or it is never
there. So this is a separate, much smaller computation -- one az/el per
satellite, plus how far that point wanders over a day for the ones whose orbit
has picked up some inclination.

WHICH SATELLITES is not a hand-kept list. Slots get reassigned (GOES-19 took
over GOES-East from GOES-16 in 2025) and a table of longitudes typed from memory
would be wrong within a year and nobody would notice. Instead: every object in
Celestrak's `weather` group whose mean motion is one revolution per sidereal
day is listed, and its longitude is computed from its own elements.
`geo-satellites.json` only ANNOTATES the ones we know something about
(downlinks, role); a satellite with no annotation still appears.

Nothing here touches the radio. The direct-broadcast downlinks are around
1.69 GHz, which no radio in this shack receives; this is a pointing aid.

    python geo.py                 # table for the configured QTH
    python geo.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from skyfield.api import load, wgs84

import passes as engine

HERE = Path(__file__).resolve().parent
DEFAULT_NOTES = HERE / "geo-satellites.json"
DEFAULT_TLE = HERE / "weather.tle"

WEATHER_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
# No callsign or address in the User-Agent: this is sent to a third party.
UA = "shack-track/1.0"
FETCH_TIMEOUT_S = 25
MIN_TLES = 10                    # fewer than this is an error page, not a thin day
REFRESH_AFTER_S = 12 * 3600      # Celestrak asks for no more than one fetch per 2 h
RETRY_AFTER_S = 30 * 60          # after a FAILED fetch, do not hammer

# One revolution per sidereal day is 1.0027 rev/day. The band is wide enough
# for a bird being moved between slots and far too narrow for anything else in
# the weather group (the lowest non-geo there is ~14 rev/day).
GEO_REV_PER_DAY = (0.99, 1.01)

# A satellite whose elevation moves less than this over a day is "fixed" for
# a small dish; beyond it the operator needs to know it wanders.
STEADY_DEG = 0.5


def rev_per_day(sat) -> float:
    return float(sat.model.no_kozai) * 1440.0 / (2.0 * math.pi)


def is_geostationary(sat) -> bool:
    lo, hi = GEO_REV_PER_DAY
    return lo < rev_per_day(sat) < hi


def slot_label(lon_deg: float) -> str:
    return f"{abs(lon_deg):.1f}°{'W' if lon_deg < 0 else 'E'}"


# --- keeping the element file fresh ---------------------------------------------

_fetch_lock = threading.Lock()
_last_attempt = 0.0
_last_error = ""


def _count_tles(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("1 "))


def ensure_tle(path: Path, url: str = WEATHER_URL, fetch=None) -> dict:
    """Refresh `path` from Celestrak when it is missing or over 12 h old.

    Judged by TLE COUNT, never HTTP status: Celestrak answers an unknown group
    with 200 and a sentence (see keps-watch.py). A failed or empty fetch leaves
    the existing file alone and says so -- stale elements for a geostationary
    bird are still good to a fraction of a degree for weeks, so the old file is
    worth far more than no file.

    Returns {"state": fresh|fetched|stale|missing, "detail": str}.
    """
    global _last_attempt, _last_error
    age = (time.time() - path.stat().st_mtime) if path.exists() else None
    if age is not None and age < REFRESH_AFTER_S:
        return {"state": "fresh", "detail": ""}
    with _fetch_lock:
        if time.monotonic() - _last_attempt < RETRY_AFTER_S and _last_attempt:
            return {"state": "stale" if path.exists() else "missing",
                    "detail": _last_error or "waiting before the next fetch"}
        _last_attempt = time.monotonic()
        try:
            if fetch is None:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                    body = r.read().decode("utf-8", "replace")
            else:
                body = fetch(url)
            n = _count_tles(body)
            if n < MIN_TLES:
                first = body.strip().splitlines()[0][:70] if body.strip() else "(empty)"
                raise ValueError(f"only {n} TLEs came back: {first}")
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body.replace("\r\n", "\n"))
            os.replace(tmp, path)
            _last_error = ""
            return {"state": "fetched", "detail": f"{n} TLEs"}
        except Exception as e:  # noqa: BLE001 -- report anything, keep the old file
            _last_error = f"{type(e).__name__}: {e}"
            return {"state": "stale" if path.exists() else "missing", "detail": _last_error}


# --- the computation --------------------------------------------------------------

def load_notes(path: Path) -> list[dict]:
    """Annotations, matched by case-insensitive PREFIX on the TLE name."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("satellites", [])


def _note_for(name: str, notes: list[dict]) -> dict:
    n = name.lower()
    # Longest prefix wins, so "GOES 19" is not shadowed by a general "GOES" row.
    best = None
    for row in notes:
        p = str(row.get("tle", "")).lower()
        if p and n.startswith(p) and (best is None or len(p) > len(best.get("tle", ""))):
            best = row
    return best or {}


def compute(qth: dict, min_el: float, tle_path: Path, notes: list[dict] | None = None,
            now: datetime | None = None) -> dict:
    """Every geostationary satellite in `tle_path`, as seen from `qth` now.

    `visible` means at or above `min_el` right now; `always_visible` means it
    stays there through its whole daily wander. A bird below the horizon is
    still returned (flagged) so the page can say how many it is not showing.
    """
    ts = load.timescale()
    now = now or datetime.now(timezone.utc)
    t = ts.from_datetime(now)
    # 25 samples across one day: enough to bound a sinusoidal daily wander.
    day = ts.tt_jd(t.tt + np.linspace(0.0, 1.0, 25))
    site = wgs84.latlon(qth["lat"], qth["lon"], elevation_m=qth.get("alt_m", 0))
    tles = engine.parse_tles(tle_path, ts)
    notes = notes or []

    sats = []
    for name, sat in tles.items():
        if not is_geostationary(sat):
            continue
        diff = sat - site
        alt, az, dist = diff.at(t).altaz()
        el_now, az_now, rng = float(alt.degrees), float(az.degrees), float(dist.km)
        alts, azs, _ = diff.at(day).altaz()
        els = np.asarray(alts.degrees, dtype=float)
        # Azimuth swing relative to now, so a bird due north does not read as 359°.
        swing = (np.asarray(azs.degrees, dtype=float) - az_now + 180.0) % 360.0 - 180.0
        sp = wgs84.subpoint(sat.at(t))
        lon = float(sp.longitude.degrees)
        note = _note_for(name, notes)
        downlinks = note.get("downlinks", [])
        entry = {
            "name": name, "display": note.get("display", name),
            "norad": int(sat.model.satnum),
            "role": note.get("role", ""), "note": note.get("note", ""),
            "source": note.get("source", ""),
            "downlinks": downlinks,
            "lon": round(lon, 2), "slot": slot_label(lon),
            "lat": round(float(sp.latitude.degrees), 2),
            "az": round(az_now, 1), "el": round(el_now, 1),
            "range_km": round(rng, 0),
            "el_min": round(float(els.min()), 1), "el_max": round(float(els.max()), 1),
            "az_swing": round(float(np.abs(swing).max()), 1),
            "inclination_deg": round(math.degrees(sat.model.inclo), 2),
            "epoch": engine._iso(sat.epoch),
            # bool(): NumPy comparisons give numpy.bool_, which Flask will not serialise.
            "above_horizon": bool(el_now > 0.0),
            "visible": bool(el_now >= min_el),
            "always_visible": bool(els.min() >= min_el),
            "steady": bool(els.max() - els.min() <= STEADY_DEG),
        }
        if downlinks and downlinks[0].get("mhz"):
            f_hz = float(downlinks[0]["mhz"]) * 1e6
            entry["path_loss_db"] = round(
                20 * math.log10(rng * 1000.0) + 20 * math.log10(f_hz) - 147.55, 1)
        sats.append(entry)

    # In view first, highest first; then the ones that are not, by how close.
    sats.sort(key=lambda s: (not s["visible"], -s["el"]))
    epochs = [s["epoch"] for s in sats]
    return {
        "generated": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "qth": qth, "min_elevation_deg": min_el,
        "tle_file": str(tle_path), "tle_count": len(tles),
        "tle_newest_epoch": max(epochs) if epochs else None,
        "tle_oldest_epoch": min(epochs) if epochs else None,
        "visible_count": sum(1 for s in sats if s["visible"]),
        "sats": sats,
    }


def _table(result: dict) -> str:
    # ASCII only: this prints on a cp1252 Windows console as well as the hub.
    rows = [f"{'satellite':24} {'slot':>8} {'az':>6} {'el':>6} {'el range 24h':>14}  role"]
    for s in result["sats"]:
        mark = " " if s["visible"] else "-"
        slot = s["slot"].replace("°", "")
        rows.append(f"{mark}{s['display'][:23]:23} {slot:>8} {s['az']:>6.1f} {s['el']:>6.1f} "
                    f"{s['el_min']:>6.1f}..{s['el_max']:<6.1f}  {s['role']}")
    rows.append(f"{result['visible_count']} of {len(result['sats'])} at or above "
                f"{result['min_elevation_deg']} deg ('-' = not in view)")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sats", type=Path, default=engine.DEFAULT_SATS)
    ap.add_argument("--tle", type=Path, default=DEFAULT_TLE)
    ap.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = engine.load_config(a.sats)
        fetch = {"state": "skipped", "detail": ""} if a.no_fetch else ensure_tle(a.tle)
        result = compute(cfg["qth"], cfg.get("min_elevation_deg", 5.0), a.tle, load_notes(a.notes))
    except (OSError, ValueError) as e:
        print(f"geo: {e}", file=sys.stderr)
        return 2
    result["fetch"] = fetch
    if a.json:
        json.dump(result, sys.stdout, indent=1)
        print()
    else:
        print(_table(result))
        if fetch["detail"]:
            print(f"elements: {fetch['state']} ({fetch['detail']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
