#!/usr/bin/env python3
"""test_rotor_server.py -- the rotator as the server exposes it: setup, arm, live status.

Runs the real Flask app against a temporary satellite table and the simulated
rotator's rotctld port. What must hold:

  * a rotator address is validated like the radio's, and cannot BE the radio
    or the receiver (position commands must never reach a transceiver)
  * saving an address moves nothing and arms nothing
  * arming is refused with no rotator, with an unreachable one, and when the
    server is read-only
  * /api/live carries the rotator's own position, and says "not configured"
    when there is none

    python test_rotor_server.py
"""

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="shacktrack-rotor-"))
SATS = tmp / "satellites.json"
shutil.copy(HERE / "satellites.json", SATS)
os.environ["SHACKTRACK_SATS"] = str(SATS)
os.environ["SHACKTRACK_STATE"] = str(tmp / "tracker-state.json")
os.environ["SHACKTRACK_RIGCTL"] = "127.0.0.1:1"
os.environ["SHACKTRACK_RECEIVER"] = ""
os.environ["SHACKTRACK_ROTOR"] = ""
os.environ["SHACKTRACK_KEPS"] = ""

sys.path.insert(0, str(HERE))
import rotor_sim as SIM  # noqa: E402
import server  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


model = SIM.RotorModel(az=123, el=45)
sim = SIM.Server(("127.0.0.1", 0), model, handler=SIM.RotctlHandler)
threading.Thread(target=sim.serve_forever, daemon=True).start()
ROT = "127.0.0.1:%d" % sim.server_address[1]
c = server.app.test_client()

# ---- nothing set up -------------------------------------------------------------
server.rotor_follower.step()
r = c.get("/api/rotor").get_json()
check("with no rotator set up the status says not configured", r["configured"] is False and r["state"] == "off", str(r))
r = c.post("/api/rotor", json={"arm": True})
check("and arming is refused with a sentence", r.status_code == 409 and "No rotator is set up" in r.get_json()["error"],
      str(r.get_json()))
check("the setup view shows no rotator", c.get("/api/setup").get_json()["rotor"]["target"] == "")

# ---- validation --------------------------------------------------------------------
def refuses(body, needle):
    try:
        server.validate_setup(body)
    except ValueError as e:
        return needle in str(e), str(e)
    return False, "accepted"


ok, why = refuses({"rotor": "not an address"}, "rotator address is host:port")
check("a malformed rotator address is refused, in the rotator's words", ok, why)
ok, why = refuses({"rotor": "127.0.0.1:1"}, "cannot be the same address as the radio")
check("the rotator cannot be the radio's address", ok, why)
ok, why = refuses({"receiver": "10.0.0.9:4533", "rotor": "10.0.0.9:4533"}, "cannot be the same address")
check("nor the receiver's, when both are saved together", ok, why)
check("a good address is accepted", server.validate_setup({"rotor": ROT}) == {"rotor": ROT})
check("an empty field means no rotator", server.validate_setup({"rotor": ""}) == {"rotor": ""})

# ---- elevation limits ---------------------------------------------------------------
check("limits default to 0 and 90 with nothing saved", server.rotor_follower.limits() == (0.0, 90.0),
      str(server.rotor_follower.limits()))
check("the setup view shows the defaults", (c.get("/api/setup").get_json()["rotor"]["el_min"],
                                            c.get("/api/setup").get_json()["rotor"]["el_max"]) == (0.0, 90))
ok, why = refuses({"rotor_el_min": "60"}, "between 0 and 45")
check("a lowest elevation above 45 is refused", ok, why)
ok, why = refuses({"rotor_el_min": "low"}, "must be a number")
check("so is one that is not a number", ok, why)
ok, why = refuses({"rotor_el_max": "135"}, "90 or 180")
check("elevation travel is 90 or 180, nothing between", ok, why)
check("good limits are accepted", server.validate_setup({"rotor_el_min": "7.5", "rotor_el_max": "180"})
      == {"rotor_el_min": 7.5, "rotor_el_max": 180})
r = c.post("/api/setup", json={"rotor_el_min": "7.5", "rotor_el_max": "180"})
check("and saved", r.status_code == 200, str(r.get_json())[:200])
check("the follower reads them on its next step, with no restart",
      server.rotor_follower.limits() == (7.5, 180.0), str(server.rotor_follower.limits()))
v = c.get("/api/setup").get_json()["rotor"]
check("and the setup page shows them back", (v["el_min"], v["el_max"]) == (7.5, 180), str(v))
c.post("/api/setup", json={"rotor_el_min": "0", "rotor_el_max": "90"})
check("they can be put back", server.rotor_follower.limits() == (0.0, 90.0))

# ---- the test button reads and moves nothing ------------------------------------------
r = c.post("/api/setup/rotor-test", json={"rotor": ROT}).get_json()
check("the setup test reads the position", r.get("reachable") and (r["az"], r["el"]) == (123.0, 45.0), str(r))
check("and sent nothing that moves", not model.moving())
r = c.post("/api/setup/rotor-test", json={"rotor": ""})
check("the test refuses an empty address", r.status_code == 400)
r = c.post("/api/setup/rotor-test", json={"rotor": "127.0.0.1:9"}).get_json()
check("the test says unreachable for a dead address", r.get("reachable") is False, str(r))

# ---- saving ---------------------------------------------------------------------------------
r = c.post("/api/setup", json={"rotor": ROT})
check("saving the rotator address works", r.status_code == 200, str(r.get_json())[:200])
check("and it is in the saved file", ROT in SATS.read_text(encoding="utf-8"))
check("the satellites are still there after the save", '"satellites"' in SATS.read_text(encoding="utf-8")
      or '"sats"' in SATS.read_text(encoding="utf-8") or "RS-44" in SATS.read_text(encoding="utf-8"))
server.rotor_follower.step()
st = c.get("/api/rotor").get_json()
check("saved, it is read: configured, reachable, at its real position",
      st["configured"] and st["reachable"] and (st["az"], st["el"]) == (123.0, 45.0), str(st))
check("saving did NOT arm it", st["armed"] is False and st["state"] == "idle", str(st))
check("and nothing moved", not model.moving() and model.az == 123.0)

# ---- live status ---------------------------------------------------------------------------------
live = c.get("/api/live").get_json()
ro = live.get("rotor") or {}
check("/api/live carries the rotator's position for the page", ro.get("present") is True and ro.get("az") == 123.0, str(ro))
check("and whether arming is allowed on this server", ro.get("enabled") is True, str(ro))

# ---- arm / disarm ---------------------------------------------------------------------------------
r = c.post("/api/rotor", json={"arm": "yes"})
check("arm must be a real true or false", r.status_code == 400)
r = c.post("/api/rotor", json={})
check("and must be present", r.status_code == 400)
r = c.post("/api/rotor", json={"arm": True})
check("arming a reachable rotator works", r.status_code == 200 and r.get_json()["armed"] is True, str(r.get_json()))
server.rotor_follower.step()
check("armed with nothing engaged, it does not move", not model.moving()
      and c.get("/api/rotor").get_json()["state"] == "armed")
r = c.post("/api/rotor", json={"arm": False}).get_json()
check("disarming works and the stop was acknowledged", r["armed"] is False and r["stop_acknowledged"] is True, str(r))

# ---- read-only server ---------------------------------------------------------------------------------
server.TRACK_ENABLED = False
r = c.post("/api/rotor", json={"arm": True})
check("a read-only server refuses to arm", r.status_code == 403)
check("and tells the page so, to hide the button", c.get("/api/rotor").get_json()["enabled"] is False)
server.TRACK_ENABLED = True

# ---- unreachable ---------------------------------------------------------------------------------
sim.shutdown(); sim.server_close()
server.rotor_follower.step()
st = c.get("/api/rotor").get_json()
check("when the rotator goes away the status says unreachable and invents no position",
      st["state"] == "unreachable" and "az" not in st, str(st))
r = c.post("/api/rotor", json={"arm": True})
check("and it cannot be armed", r.status_code == 409 and "not answering" in r.get_json()["error"])

# ---- clearing ---------------------------------------------------------------------------------
c.post("/api/setup", json={"rotor": ""})
server.rotor_follower.step()
check("clearing the address returns it to not configured", c.get("/api/rotor").get_json()["configured"] is False)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
