#!/usr/bin/env python3
"""test_setup.py -- the station setup page's API, with no radio.

Runs against a temporary copy of satellites.json, never the real one:

  * reading the settings shows the position as grid AND lat/lon
  * a grid or a lat/lon saves, and the other form is derived and shown
  * bad input refuses the WHOLE save, with a sentence, and changes nothing
  * a save keeps the _comment block and every satellite entry
  * the saved radio address wins over the environment default, live
  * clearing the radio address falls back to the default
  * the radio test reads f and m from the address given, and says so when
    nothing answers

    python test_setup.py
"""

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="shacktrack-setup-"))
SATS = tmp / "satellites.json"
shutil.copy(HERE / "satellites.json", SATS)
os.environ["SHACKTRACK_SATS"] = str(SATS)
os.environ["SHACKTRACK_STATE"] = str(tmp / "tracker-state.json")
os.environ["SHACKTRACK_RIGCTL"] = "127.0.0.1:1"        # a default nothing answers on
os.environ["SHACKTRACK_KEPS"] = ""

sys.path.insert(0, str(HERE))
import server  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fails.append(name)


class FakeRig(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().strip()
        self.wfile.write(b"145800000\n" if line == b"f" else b"FM\n15000\n" if line == b"m" else b"RPRT -1\n")


rig = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeRig)
threading.Thread(target=rig.serve_forever, daemon=True).start()
RIG = "127.0.0.1:%d" % rig.server_address[1]

c = server.app.test_client()
original = json.loads(SATS.read_text(encoding="utf-8"))

print("setup page")

# --- 1. read ----------------------------------------------------------------
r = c.get("/api/setup")
v = r.get_json()
check("GET /api/setup answers", r.status_code == 200, r.status_code)
check("the position is shown as a grid too", v["qth"].get("grid") == "II22TB", v["qth"])
check("the radio is the environment default until one is saved",
      v["rigctl"] == {"target": "127.0.0.1:1", "source": "default", "default": "127.0.0.1:1"}, v["rigctl"])
check("GET /setup serves the page", c.get("/setup").status_code == 200)

# --- 2. bad input refuses everything ----------------------------------------
before = SATS.read_bytes()
for body, why in [
    ({"grid": "ZZ99"}, "a grid outside A-R"),
    ({"grid": "FM18RQ", "lat": 1, "lon": 2}, "a grid AND lat/lon"),
    ({"lat": 38.7}, "latitude without longitude"),
    ({"lat": 91, "lon": 0}, "latitude out of range"),
    ({"min_elevation_deg": 60}, "minimum elevation out of range"),
    ({"alt_m": "high"}, "a height that is not a number"),
    ({"rigctl": "aurora"}, "a radio address without a port"),
    ({"rigctl": "host:99999"}, "a port out of range"),
    ({"rigctl": "a b:4532"}, "a space in the host"),
    ({"grid": "FM18RQ", "rigctl": "nope"}, "one good field and one bad"),
]:
    r = c.post("/api/setup", json=body)
    j = r.get_json()
    check("refused: " + why, r.status_code == 400 and not j["ok"] and j["error"].endswith("."), j)
check("nothing was written by any refused save", SATS.read_bytes() == before)
check("an empty save is refused", c.post("/api/setup", json={}).status_code == 400)
check("a non-JSON save is refused", c.post("/api/setup", data="x").status_code == 400)

# --- 3. save a grid ----------------------------------------------------------
r = c.post("/api/setup", json={"grid": "fm18rq", "alt_m": "29", "min_elevation_deg": 10})
j = r.get_json()
check("a grid saves", r.status_code == 200 and j["ok"], j)
check("the grid is stored upper case, at its centre",
      j["qth"]["name"] == "FM18RQ" and abs(j["qth"]["lat"] - 38.6875) < 1e-3
      and abs(j["qth"]["lon"] + 76.5417) < 1e-3, j["qth"])
check("height and minimum elevation saved", j["qth"]["alt_m"] == 29 and j["min_elevation_deg"] == 10)
saved = json.loads(SATS.read_text(encoding="utf-8"))
check("the _comment block survives a save", saved.get("_comment") == original["_comment"])
check("every satellite entry survives a save", saved["satellites"] == original["satellites"])
check("the previous file is kept as .bak",
      json.loads((tmp / "satellites.json.bak").read_text(encoding="utf-8"))["qth"]["name"] == "II22TB")
check("the pass engine reads the new position",
      server.engine.load_config(SATS)["qth"]["name"] == "FM18RQ")

# --- 4. save a lat/lon -------------------------------------------------------
r = c.post("/api/setup", json={"lat": "38.7011", "lon": "-76.5527"})
j = r.get_json()
check("a lat/lon saves and its grid is derived",
      r.status_code == 200 and j["qth"]["name"] == "FM18RQ" and j["qth"]["lat"] == 38.7011, j)
check("height is kept when only the position changes", j["qth"]["alt_m"] == 29)

# --- 5. the radio address ----------------------------------------------------
check("the default radio is unreachable", server._read_radio()["reachable"] is False)
r = c.post("/api/setup", json={"rigctl": RIG})
check("a radio address saves", r.status_code == 200 and r.get_json()["rigctl"]["source"] == "setup")
rd = server._read_radio()
check("the saved address wins over the default, without a restart",
      rd["reachable"] and rd["target"] == RIG and rd["hz"] == 145800000 and rd["mode"] == "FM", rd)
r = c.post("/api/setup", json={"rigctl": ""})
check("clearing it goes back to the default",
      r.get_json()["rigctl"] == {"target": "127.0.0.1:1", "source": "default", "default": "127.0.0.1:1"}
      and "rigctl" not in json.loads(SATS.read_text(encoding="utf-8")))

# --- 6. the radio test -------------------------------------------------------
t = c.post("/api/setup/radio-test", json={"rigctl": RIG}).get_json()
check("the radio test reads f and m from the address given",
      t["reachable"] and t["hz"] == 145800000 and t["mode"] == "FM", t)
t = c.post("/api/setup/radio-test", json={"rigctl": "127.0.0.1:1"}).get_json()
check("the radio test says so when nothing answers", t["reachable"] is False and t["target"] == "127.0.0.1:1", t)
r = c.post("/api/setup/radio-test", json={"rigctl": "not an address"})
check("the radio test refuses a malformed address", r.status_code == 400)
check("the radio test saves nothing", "rigctl" not in json.loads(SATS.read_text(encoding="utf-8")))

rig.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d failed" % len(fails) if fails else "\nall passed")
sys.exit(1 if fails else 0)
