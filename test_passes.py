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
