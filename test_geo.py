#!/usr/bin/env python3
"""test_geo.py -- the geostationary weather list, no network, no radio.

    python test_geo.py

Uses test-fixtures/weather-2026-11-16.tle (five geostationary birds and three
low-orbit ones, cut from Celestrak's weather group) at a fixed instant, so the
answers mean the same thing next year.

The pointing is checked against the textbook closed-form look angle for a
satellite on the equator, NOT against Skyfield's own numbers: a test that
recomputes the answer the same way the code does can only ever agree with it.
"""

import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import geo
import passes as engine

TLE = HERE / "test-fixtures" / "weather-2026-11-16.tle"
NOTES = geo.load_notes(HERE / "geo-satellites.json")
T0 = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
MD = {"name": "FM18SJ", "lat": 38.3758, "lon": -76.4154, "alt_m": 19}

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def textbook(lat, lon, sat_lon):
    """Az/el to a point on the geostationary arc, spherical Earth."""
    la, dl = math.radians(lat), math.radians(sat_lon - lon)
    cg = math.cos(la) * math.cos(dl)
    el = math.degrees(math.atan2(cg - 6378.137 / 42164.0, math.sqrt(1 - cg * cg)))
    az = (math.degrees(math.atan2(math.sin(dl), -math.sin(la) * math.cos(dl))) + 360) % 360
    return az, el


r = geo.compute(MD, 5.0, TLE, NOTES, now=T0)
by = {s["name"]: s for s in r["sats"]}

# 1. Only the geostationary birds are listed. The fixture holds three that are
#    not (two Meteors, one NOAA); listing any of them would hand the operator a
#    fixed bearing for something crossing the sky in twelve minutes.
check("five geostationary birds listed", len(r["sats"]) == 5, str(sorted(by)))
check("no low-orbit bird listed",
      not any(n.startswith(("METEOR", "NOAA")) for n in by), str(sorted(by)))

# 2. Slots come from the elements, not from a table.
g19, g18 = by["GOES 19"], by["GOES 18"]
check("GOES-19 computed near 75.2 W", abs(g19["lon"] + 75.2) < 0.3, str(g19["lon"]))
check("GOES-18 computed near 137 W", abs(g18["lon"] + 137.0) < 0.5, str(g18["lon"]))
check("slot label reads west", g19["slot"].endswith("°W"), g19["slot"])

# 3. Pointing agrees with the textbook formula, fed the computed longitude.
#    Half a degree covers Earth's flattening and the birds' small inclination.
for s in (g19, g18, by["GOES 16"]):
    az, el = textbook(MD["lat"], MD["lon"], s["lon"])
    daz = abs((s["az"] - az + 180) % 360 - 180)
    check(f"{s['display']} elevation matches textbook", abs(s["el"] - el) < 0.5,
          f"{s['el']} vs {el:.2f}")
    check(f"{s['display']} azimuth matches textbook", daz < 0.5, f"{s['az']} vs {az:.2f}")

# 4. From Maryland GOES-East is high and almost due south; GOES-West is low in
#    the south-west. A sign error in longitude would put East in the west.
check("GOES-19 roughly south and high", 170 < g19["az"] < 190 and 40 < g19["el"] < 50,
      f"az {g19['az']} el {g19['el']}")
check("GOES-18 south-west and low", 240 < g18["az"] < 260 and 8 < g18["el"] < 20,
      f"az {g18['az']} el {g18['el']}")

# 5. Visibility. Himawari is on the far side of the planet from Maryland.
him = by["HIMAWARI-9"]
check("Himawari-9 below the horizon and not visible",
      him["el"] < 0 and not him["visible"] and not him["above_horizon"], str(him["el"]))
check("GOES-19 visible and always visible", g19["visible"] and g19["always_visible"])
check("visible birds sort before hidden ones",
      [s["visible"] for s in r["sats"]] == sorted((s["visible"] for s in r["sats"]), reverse=True))
check("visible_count matches", r["visible_count"] == sum(s["visible"] for s in r["sats"]))

# 6. The minimum elevation is the operator's, and it bites. GOES-18 is ~14 deg up.
r20 = geo.compute(MD, 20.0, TLE, NOTES, now=T0)
check("GOES-18 hidden at a 20 deg limit",
      not {s["name"]: s for s in r20["sats"]}["GOES 18"]["visible"])

# 7. Wander. Meteosat-9 is inclined ~9.7 deg; GOES-19 is parked.
#    Seen from the Meteosat's own longitude so it is well above the horizon.
af = geo.compute({"name": "x", "lat": 0.0, "lon": 45.0, "alt_m": 0}, 5.0, TLE, NOTES, now=T0)
m9 = {s["name"]: s for s in af["sats"]}["METEOSAT-9 (MSG-2)"]
check("inclined bird is flagged as wandering", not m9["steady"],
      f"el {m9['el_min']}..{m9['el_max']}")
check("its daily elevation range is degrees wide", m9["el_max"] - m9["el_min"] > 5,
      f"{m9['el_max'] - m9['el_min']:.1f}")
check("GOES-19 is steady", g19["steady"], f"el {g19['el_min']}..{g19['el_max']}")

# 8. Annotations: known birds carry downlinks, unknown ones still appear.
check("GOES-19 annotated as GOES-East with HRIT at 1694.1",
      g19["role"] == "GOES-East" and g19["downlinks"][0]["mhz"] == 1694.1)
check("path loss at 1694.1 MHz is the right order", 187 < g19["path_loss_db"] < 190,
      str(g19.get("path_loss_db")))
check("unannotated bird is listed with no downlinks",
      him["downlinks"] == [] and "path_loss_db" not in him)

# 9. The whole result must serialise: numpy.bool_ leaking out is a 500 on the hub.
try:
    json.dumps(r)
    check("result is plain JSON", True)
except TypeError as e:
    check("result is plain JSON", False, str(e))

# 10. Element refresh: judged by content, and a bad fetch never destroys a good file.
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "weather.tle"
    good = TLE.read_text(encoding="utf-8") * 2          # 16 records
    geo._last_attempt = 0.0
    st = geo.ensure_tle(p, fetch=lambda url: good)
    check("a missing file is fetched", st["state"] == "fetched" and p.exists(), str(st))
    st = geo.ensure_tle(p, fetch=lambda url: (_ for _ in ()).throw(AssertionError("fetched a fresh file")))
    check("a fresh file is not fetched again", st["state"] == "fresh", str(st))

    old = time.time() - geo.REFRESH_AFTER_S - 60
    os.utime(p, (old, old))
    before = p.read_text(encoding="utf-8")
    geo._last_attempt = 0.0
    st = geo.ensure_tle(p, fetch=lambda url: "Invalid query: \"GROUP=weather\" not found")
    check("a 200-with-an-error-body is refused", st["state"] == "stale", str(st))
    check("and the old file is left intact", p.read_text(encoding="utf-8") == before)
    st = geo.ensure_tle(p, fetch=lambda url: good)
    check("no immediate retry after a failure", st["state"] == "stale", str(st))
    geo._last_attempt = 0.0
    st = geo.ensure_tle(p, fetch=lambda url: good)
    check("a stale file is replaced once the wait is over", st["state"] == "fetched", str(st))

# 11. The Meteor birds ride in the ordinary pass list via EXTRA_TLES, and carry
#     no downlink -- so the tracker can never try to tune a ham radio to 137 MHz.
cfg = engine.load_config(HERE / "satellites.json")
cfg["qth"] = MD
saved = engine.EXTRA_TLES
try:
    engine.EXTRA_TLES = []
    without = engine.compute(cfg, HERE / "amateur.tle", 24.0, now=T0)
    check("without the weather file the Meteors are reported missing",
          {"METEOR-M2-3", "METEOR-M2-4"} <= set(without["missing_from_tle"]),
          str(without["missing_from_tle"]))
    engine.EXTRA_TLES = [TLE]
    with_wx = engine.compute(cfg, HERE / "amateur.tle", 24.0, now=T0)
    met = [p for p in with_wx["passes"] if p["sat"].startswith("METEOR")]
    check("with it they have passes", len(met) >= 4, f"{len(met)} passes")
    check("Meteor passes carry no downlink to tune to",
          all(p["downlink_khz"] is None and p["uplink_khz"] is None for p in met))
    check("the amateur birds are unaffected",
          [p["aos"] for p in with_wx["passes"] if not p["sat"].startswith("METEOR")]
          == [p["aos"] for p in without["passes"]])
    engine.EXTRA_TLES = [HERE / "no-such-file.tle"]
    try:
        engine.compute(cfg, HERE / "amateur.tle", 1.0, now=T0)
        check("a missing extra file is skipped, not fatal", True)
    except OSError as e:
        check("a missing extra file is skipped, not fatal", False, str(e))
finally:
    engine.EXTRA_TLES = saved

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
