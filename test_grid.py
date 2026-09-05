"""The public grid predictor: locator maths, refusal of bad input, cache bound.

Socket-free and radio-free. The engine is exercised only where a real pass
computation is the point; everything else drives the pure functions.
"""
import sys

import passes as engine
import server

failures = 0


def check(cond, label):
    global failures
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures += 1


print("grid predictor")

# --- 1. the square CENTRE, not its corner ---------------------------------
# Taking the corner would place a station up to 3 km from where they said they
# were -- the same class of error that had this project's own QTH one
# subsquare east for months.
lat, lon = engine.grid_to_latlon("II22TB")
check(abs(lat - -7.9375) < 1e-4 and abs(lon + 14.375) < 1e-4,
      "II22TB resolves to the station position, to 4 decimals")

# --- 2. round-trips against the forward function --------------------------
for g in ("II22TB", "JN58TD", "FN31PR", "AA00AA", "RR73XX"):
    la, lo = engine.grid_to_latlon(g)
    check(engine.maidenhead(la, lo).upper() == g,
          "%s survives grid -> lat/lon -> grid" % g)

# A 4-character grid resolves to the middle of its field, so it round-trips
# to the CENTRE subsquare rather than to itself.
la, lo = engine.grid_to_latlon("IO91")
check(engine.maidenhead(la, lo).upper().startswith("IO91"),
      "a 4-character grid resolves inside its own field")

# --- 3. bad input is REFUSED, never silently defaulted --------------------
# A typo that quietly computes somewhere else is the Copenhagen failure: a
# wrong-by-6500 km prediction that looks entirely plausible on screen.
for bad in ("", "FM", "II22TBK", "ZZ99", "II22ZZ", "1234", "FM1_SJ",
            "../etc/passwd", "II22TB\x00", "  ", "II22S"):
    try:
        engine.grid_to_latlon(bad)
        check(False, "rejects %r" % bad)
    except ValueError as e:
        check(bool(str(e)) and str(e)[0].isupper(),
              "rejects %r with a sentence the page can show" % bad)

# --- 4. case and whitespace are forgiving ---------------------------------
a = engine.grid_to_latlon("ii22tb")
b = engine.grid_to_latlon("  II22TB  ")
c = engine.grid_to_latlon("II22tb")
check(a == b == c, "case and surrounding whitespace do not change the answer")

# --- 5. a visitor's grid cannot alter the operator's own config -----------
before = engine.load_config(server.SATS)["qth"]
cfg = server._grid_cfg("JN58TD")
after = engine.load_config(server.SATS)["qth"]
check(cfg["qth"]["name"] == "JN58TD", "the request config carries the visitor's grid")
check(after == before, "and the station's own QTH on disk is untouched")
check(cfg["satellites"] is not None and len(cfg["satellites"]) > 0,
      "the satellite table is still present for the visitor")

# --- 6. the grid cache is bounded ----------------------------------------
# ?grid= is the one parameter a stranger controls, so without a cap they could
# mint a new cache key -- and a ~280 kB, ~2.6 s pass list -- on every request.
with server._lock:
    server._cache.clear()
    server._grid_seen.clear()
for i in range(server.GRID_CACHE_MAX * 3):
    k = server._grid_key("FM%02d%s%s" % (i % 100, chr(65 + i % 24), chr(65 + (i * 7) % 24)))
    with server._lock:
        server._cache[k] = (0.0, "payload")
with server._lock:
    held = [k for k in server._cache if k.startswith("passes:grid:")]
check(len(held) <= server.GRID_CACHE_MAX,
      "%d distinct grids leave at most %d cached" % (server.GRID_CACHE_MAX * 3,
                                                     server.GRID_CACHE_MAX))

# --- 7. a real computation for somewhere else actually differs -----------
# Guards against the endpoint quietly returning this station's passes.
from pathlib import Path
home = engine.compute(engine.load_config(server.SATS), Path(server.TLE), 6.0)
away = engine.compute(server._grid_cfg("JN58TD"), Path(server.TLE), 6.0)
check(home["qth"]["name"] != away["qth"]["name"], "the two runs report different QTHs")
same_aos = {p["aos"] for p in home["passes"]} & {p["aos"] for p in away["passes"]}
check(len(same_aos) < max(1, len(home["passes"])),
      "Munich does not get Maryland's pass times")

print("test_grid: %d failure(s)" % failures)
sys.exit(1 if failures else 0)
