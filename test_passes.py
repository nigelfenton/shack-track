#!/usr/bin/env python3
"""test_passes.py -- pin the pass engine's edge cases, no network, no radio.

    python test_passes.py

Uses the amateur.tle checked in beside it, so the passes are fixed in time; the
window is anchored to that file's epoch rather than to "now" so the test means
the same thing next year.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import passes as engine

HERE = Path(__file__).resolve().parent
CFG = engine.load_config(HERE / "satellites.json")
TLE = HERE / "amateur.tle"

# A fixed instant a day after the checked-in elements, so SGP4 is well inside
# its accuracy and the answers do not drift with the wall clock.
T0 = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def run(hours, now=T0, **kw):
    return engine.compute(CFG, TLE, hours, now=now, **kw)


# 1. Every window length the page offers, plus short ones, must compute.
for h in (1, 2, 3, 6, 12, 24, 48):
    try:
        r = run(h)
        check(f"{h} h window computes", True, f"{len(r['passes'])} passes")
    except Exception as e:  # noqa: BLE001
        check(f"{h} h window computes", False, f"{type(e).__name__}: {e}")

# 2. A window that ENDS mid-pass. Find one from a long run, then end the
#    window inside it and require the clipped pass to be reported with
#    LOS == window end. This is the 6 h case that raised TypeError.
long = run(24)
mid = next(p for p in long["passes"] if p["duration_s"] > 300)
aos = datetime.fromisoformat(mid["aos"].replace("Z", "+00:00"))
end = aos + timedelta(seconds=mid["duration_s"] // 2)
hours = (end - T0).total_seconds() / 3600
r = run(hours)
clipped = [p for p in r["passes"] if p["clipped"]]
check("window ending mid-pass reports the clipped pass",
      any(p["sat"] == mid["sat"] for p in clipped),
      f"{mid['sat']} AOS {mid['aos']}, window end {end.isoformat()}")
check("clipped pass LOS is the window end",
      all(abs((datetime.fromisoformat(p["los"].replace("Z", "+00:00")) - end).total_seconds()) <= 1
          for p in clipped), f"{len(clipped)} clipped")

# 3. A window that STARTS mid-pass: in_progress, and the AOS is the pass's REAL
#    AOS (before the window start), not the moment we looked.
start = aos + timedelta(seconds=mid["duration_s"] // 2)
r = run(6, now=start)
live = [p for p in r["passes"] if p["in_progress"]]
check("window starting mid-pass reports it in progress",
      any(p["sat"] == mid["sat"] for p in live))
check("in-progress pass keeps its real AOS",
      any(p["sat"] == mid["sat"] and p["aos"] == mid["aos"] for p in live),
      f"expected AOS {mid['aos']}")
check("no pass that ended before the window start is listed",
      all(p["los"] > start.replace(microsecond=0).isoformat().replace("+00:00", "Z") for p in r["passes"]))
check("in-progress is the ONLY pass with AOS before the window start",
      all(p["in_progress"] == (p["aos"] < start.replace(microsecond=0).isoformat().replace("+00:00", "Z")) for p in r["passes"]))

# 4. Physics bounds nobody should ever break: elevation within [min_el, 90],
#    azimuths within [0, 360), LOS after AOS, sorted by AOS.
for p in long["passes"]:
    if not (long["min_elevation_deg"] - 0.5 <= p["peak_el"] <= 90):
        check("peak elevation in range", False, f"{p['sat']} {p['peak_el']}")
        break
    if not all(0 <= p[k] < 360 for k in ("aos_az", "peak_az", "los_az")):
        check("azimuths in range", False, f"{p['sat']}")
        break
    if p["los"] <= p["aos"]:
        check("LOS after AOS", False, f"{p['sat']}")
        break
else:
    check("peak elevation, azimuth and ordering bounds", True, f"{len(long['passes'])} passes")
check("passes sorted by AOS", long["passes"] == sorted(long["passes"], key=lambda p: p["aos"]))

# 4b. The result must survive the standard json encoder (Flask uses it): NumPy
#     scalars leaking out of Skyfield comparisons took the hub down once.
import json
try:
    json.dumps(r); json.dumps(long)
    check("results are plain-JSON serialisable", True)
except TypeError as e:
    check("results are plain-JSON serialisable", False, str(e))

# 4c. Doppler sign convention against pass-RS44-2026-08-29.csv. NOTE: despite
#     its name that file is 12 min of PRE-AOS geometry (el -50 to -29, never
#     above the horizon), so it cannot check a pass -- but its up_hz/dn_hz
#     columns encode the convention a Gpredict-driven radio was seen to follow
#     that night, and physics does not care about the horizon. Three instants,
#     engine vs file, using the same range rate the file recorded so only the
#     FORMULA is under test (its elements were a few hours older than ours).
import csv
rows = [r for r in csv.DictReader(open(HERE / "pass-RS44-2026-08-29.csv", encoding="utf-8")) if r["range_rate_kms"]]
picks = [rows[0], rows[len(rows) // 2], rows[-1]]
worst = 0
for r in picks:
    rr = float(r["range_rate_kms"])
    dn = engine.doppler(435640_000, rr, uplink=False)["hz"]
    up = engine.doppler(145965_000, rr, uplink=True)["hz"]
    worst = max(worst, abs(dn - int(r["dn_hz"])), abs(up - int(r["up_hz"])))
check("Doppler formula reproduces the recorded up/down columns", worst <= 5,
      f"worst {worst} Hz at three instants")
check("Doppler sign: approaching bird heard HIGH on the downlink",
      engine.doppler(435640_000, -3.0, uplink=False)["shift_hz"] > 0 and
      engine.doppler(145965_000, -3.0, uplink=True)["shift_hz"] < 0)
# Magnitude bound from reference_doppler_sanity_checks: |shift| <= f * 7.4/c.
check("Doppler magnitude inside the orbital bound",
      abs(engine.doppler(145800_000, 7.4, uplink=False)["shift_hz"]) < 3700)

# 4d. live_state at TCA of a computed pass: must pick that pass as live, put
#     the bird at the pass's own peak, and have range rate crossing zero there.
tca = datetime.fromisoformat(mid["tca"].replace("Z", "+00:00"))
ls = engine.live_state(CFG, TLE, now=tca)
check("live_state picks the pass in progress", ls["state"] == "live" and ls["pass"]["sat"] == mid["sat"],
      f"state={ls['state']} sat={ls['pass'] and ls['pass']['sat']}")
check("live_state geometry at TCA is the pass peak",
      abs(ls["geometry"]["el"] - mid["peak_el"]) < 0.3 and abs(ls["geometry"]["az"] - mid["peak_az"]) < 1.0,
      f"el {ls['geometry']['el']} vs peak {mid['peak_el']}, az {ls['geometry']['az']} vs {mid['peak_az']}")
check("range rate is ~0 at TCA", abs(ls["geometry"]["range_rate_kms"]) < 0.15,
      f"{ls['geometry']['range_rate_kms']} km/s")
before = engine.live_state(CFG, TLE, now=tca - timedelta(minutes=3))["geometry"]["range_rate_kms"]
after = engine.live_state(CFG, TLE, now=tca + timedelta(minutes=3))["geometry"]["range_rate_kms"]
check("range rate is negative before TCA and positive after (one reversal)", before < 0 < after,
      f"{before} -> {after} km/s")
try:
    json.dumps(ls); check("live_state is plain-JSON serialisable", True)
except TypeError as e:
    check("live_state is plain-JSON serialisable", False, str(e))

# 4e. follow_radio: the view should follow the bird the RADIO is on, because on
#     2026-09-02 it drew AO-123 and then AO-27 while the radio was on RS-44 all
#     evening. Evidence order: an explicit pin, then the radio, then the clock.
tca_mid = datetime.fromisoformat(mid["tca"].replace("Z", "+00:00"))
on_its_downlink = mid["downlink_khz"] * 1000
r_follow = engine.live_state(CFG, TLE, now=tca_mid, radio_hz=on_its_downlink)
check("follows the bird the radio is tuned to",
      r_follow["selected_by"] == "radio" and r_follow["pass"]["sat"] == mid["sat"],
      f"{r_follow['selected_by']} / {r_follow['pass']['display']}")
# A frequency that is nobody's downlink must NOT claim a bird.
r_none = engine.live_state(CFG, TLE, now=tca_mid, radio_hz=145_900_000)
check("a frequency that is no bird's downlink falls back to the schedule",
      r_none["selected_by"] == "schedule", r_none["selected_by"])
# An explicit pin outranks the radio.
r_pin = engine.live_state(CFG, TLE, now=tca_mid, radio_hz=on_its_downlink, sat="RS-44")
check("an explicit pin outranks the radio",
      r_pin["selected_by"] == "sat" and r_pin["pass"]["sat"] == "RS-44",
      f"{r_pin['selected_by']} / {r_pin['pass'] and r_pin['pass']['display']}")
# A bird below the horizon must not be followed on frequency alone.
below = engine.live_state(CFG, TLE, now=tca_mid - timedelta(hours=3), radio_hz=on_its_downlink)
check("a bird that is not up is not followed on frequency alone",
      below["selected_by"] == "schedule", below["selected_by"])

# 5. Every configured satellite resolves in the checked-in TLE.
check("every configured satellite is in the TLE", not long["missing_from_tle"],
      ", ".join(long["missing_from_tle"]) or "all present")

# 6. Unreadable inputs raise, they do not return an empty list.
try:
    engine.compute(CFG, HERE / "does-not-exist.tle", 12, now=T0)
    check("missing TLE raises", False, "returned normally")
except OSError:
    check("missing TLE raises", True)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
