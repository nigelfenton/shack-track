#!/usr/bin/env python3
"""test_rotor.py -- the rotator follower's rules, against the simulated rotator.

No hardware and no sleeping: the follower is stepped by hand against
rotor_sim's model, with the clock passed in. What must hold:

  * unarmed, it reads the rotator and NEVER moves it
  * armed with nothing engaged, it still does not move
  * it goes to the AOS bearing shortly before AOS, follows during the pass,
    and stops at LOS
  * it steps, not hunts: no new command until the bird has moved 2 degrees
  * a rotator that accepts commands and does not turn is called stalled,
    stopped and DISARMED
  * unreachable is said, not hidden, and arming an unreachable rotator is refused
  * a restart does not arm it

    python test_rotor.py
"""

import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rotor as RO
import rotor_sim as SIM

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class Rig:
    """A follower wired to the simulated rotator, with a hand-stepped clock."""

    def __init__(self, az=0.0, el=0.0, stall="", reachable=True):
        self.model = SIM.RotorModel(az=az, el=el, stall=stall)
        self.t = 1000.0
        self.sent = []
        self.reachable = reachable
        self.engaged = None
        self.live = {}
        self.log = []
        link = RO.RotctlLink(lambda: "sim:4533", transact=self._transact)
        self.f = RO.RotorFollower(link, lambda sat: self.live, lambda: self.engaged,
                                  lambda text, cls="": self.log.append((text, cls)))

    def _transact(self, cmd):
        if not self.reachable:
            return None
        if cmd != "p":
            self.sent.append(cmd)
        return self.model.rotctl(cmd, self.t)

    def run(self, seconds, live=None):
        for _ in range(int(seconds)):
            self.t += 1.0
            if live is not None:
                self.live = live(self) if callable(live) else live
            self.f.step(self.t)

    def moves(self):
        return [c for c in self.sent if c.startswith("P")]


def live_pass(az, el, sat="RS-44"):
    return {"state": "live", "pass": {"aos_az": 30.0}, "geometry": {"az": az, "el": el}, "bird": sat}


def before_aos(secs, aos_az=30.0):
    return {"state": "next", "pass": {"aos_az": aos_az}, "seconds_to_aos": secs}


# ---- parsing ----------------------------------------------------------------
check("a position reply parses", RO.parse_position("123.400000\n45.000000\n") == (123.4, 45.0))
check("an error report is not a position", RO.parse_position("RPRT -1\n") is None)
check("half a reply is not a position", RO.parse_position("123.4\n") is None)
check("nonsense is not a position", RO.parse_position("AZ=123\nEL=045\n") is None)
check("RPRT 0 is read as 0", RO.parse_report("RPRT 0\n") == 0)
check("RPRT -1 is read as -1", RO.parse_report("set_pos: 1 2\nRPRT -1\n") == -1)
check("no report is None", RO.parse_report("") is None and RO.parse_report(None) is None)
check("azimuth error takes the short way round north", RO.az_gap(350, 10) == 20 and RO.az_gap(10, 350) == 20)

# ---- unarmed: read only --------------------------------------------------------
r = Rig(az=100, el=10)
r.engaged = "RS-44"
r.run(20, live_pass(200, 40))
st = r.f.status()
check("unarmed, it reports where the rotator is", st["reachable"] and st["az"] == 100.0 and st["el"] == 10.0, str(st))
check("unarmed, it sends NOTHING that moves, even with a bird up and engaged", r.sent == [], str(r.sent))
check("and says it is not armed", st["state"] == "idle" and not st["armed"], str(st))

# ---- armed, nothing engaged ----------------------------------------------------
r = Rig(az=100, el=10)
check("arming a reachable rotator works", r.f.arm().get("ok") is True)
r.run(10, live_pass(200, 40))
check("armed with nothing engaged, it still does not move", r.moves() == [] and r.f.status()["state"] == "armed",
      str(r.sent))

# ---- a whole pass --------------------------------------------------------------
r = Rig(az=100, el=0)
r.f.arm()
r.engaged = "RS-44"
r.run(5, before_aos(900))
check("long before AOS it waits and does not move", r.moves() == [] and "AOS in 900" in r.f.status()["detail"],
      r.f.status()["detail"])
r.run(1, before_aos(200))
check("inside four minutes of AOS it goes to the AOS bearing at the horizon", r.moves() == ["P 30.0 0.0"], str(r.moves()))
check("and says it is pre-positioning", r.f.status()["state"] == "pre-positioning", r.f.status()["state"])
r.run(30, before_aos(170))
st = r.f.status()
check("it gets there, and reports the rotator's own position", st["az"] == 30.0 and "on target" in st["detail"], str(st))
check("without sending the same command again", r.moves() == ["P 30.0 0.0"], str(r.moves()))

n_before = len(r.moves())
r.run(1, live_pass(31.0, 1.0))
check("at AOS a bird 1 degree away is not worth a new command", len(r.moves()) == n_before, str(r.moves()))
r.run(1, live_pass(33.0, 2.5))
check("at 2 degrees or more it is", r.moves()[-1] == "P 33.0 2.5", str(r.moves()))
check("and it is following", r.f.status()["state"] == "following")

track = lambda rig: live_pass(33.0 + (rig.t - t0) * 0.5, min(60.0, 2.5 + (rig.t - t0) * 0.4))
t0 = r.t
r.run(100, track)
st = r.f.status()
check("through the pass it stays on the bird", st["state"] == "following" and st["error_deg"] <= 3.0, str(st))
steps = len(r.moves()) - n_before
check("in steps, not one command a second (100 s of pass, well under 100 commands)", 20 <= steps <= 40, f"{steps} commands")
check("the page gets wanted and actual bearings", "want_az" in st and "az" in st and "error_deg" in st, str(st))

r.run(1, {"state": "next", "pass": {"aos_az": 30.0}, "seconds_to_aos": 5400})
check("at LOS it sends a stop", r.sent[-1] == "S", str(r.sent[-3:]))
sent_n = len(r.sent)
r.run(10, {"state": "next", "pass": {"aos_az": 30.0}, "seconds_to_aos": 5390})
check("once, not every second", len(r.sent) == sent_n, str(r.sent[sent_n:]))
check("and stays armed for the next pass", r.f.status()["armed"] and r.f.status()["state"] == "armed")

# ---- elevation never negative; azimuth normalised ------------------------------
r = Rig(az=350, el=0)
r.f.arm(); r.engaged = "X"
r.run(1, live_pass(365.0, -3.0))
check("a bird below the horizon is sent as elevation 0, and 365 as 5", r.moves() == ["P 5.0 0.0"], str(r.moves()))

# ---- disarm ----------------------------------------------------------------------
r = Rig(az=0, el=0)
r.f.arm(); r.engaged = "X"
r.run(2, live_pass(180, 30))
out = r.f.disarm()
check("disarming sends a stop and says it was acknowledged", r.sent[-1] == "S" and out["stop_acknowledged"] is True, str(out))
az_then = r.model.az
r.run(10, live_pass(180, 30))
check("after that the rotator does not move again", r.model.az == az_then and not r.model.moving())

# ---- the engagement ends while following -----------------------------------------
r = Rig(az=0, el=0)
r.f.arm(); r.engaged = "X"
r.run(2, live_pass(180, 30))
r.engaged = None
r.run(1, live_pass(180, 30))
check("when the tracker disengages, the rotator is stopped", r.sent[-1] == "S" and r.f.status()["state"] == "armed", str(r.sent))

# ---- stalled ---------------------------------------------------------------------
r = Rig(az=10, el=0, stall="both")
r.f.arm(); r.engaged = "X"
r.run(11, live_pass(100, 20))
check("a rotator that is not turning is given a little time", r.f.status()["state"] == "following", r.f.status()["state"])
r.run(3, live_pass(100, 20))
st = r.f.status()
check("then it is called stalled", st["state"] == "stalled" and "not moving" in st["detail"], str(st))
check("stopped", r.sent[-1] == "S", str(r.sent))
check("and DISARMED, not left driving a dead motor", st["armed"] is False)
n = len(r.sent)
r.run(20, live_pass(100, 20))
check("it sends nothing more until someone arms it again", len(r.sent) == n and r.f.status()["state"] == "stalled", str(r.sent[n:]))
check("the log says so", any("stalled" in t for t, _ in r.log), str(r.log))

r = Rig(az=10, el=0, stall="el")
r.f.arm(); r.engaged = "X"
r.run(40, live_pass(60, 40))
check("one dead axis is enough: azimuth arrived, elevation did not, still stalled",
      r.f.status()["state"] == "stalled" and abs(r.model.az - 60.0) <= 1.0 and r.model.el == 0.0,
      str(r.f.status()))

r = Rig(az=0, el=0)
r.f.arm(); r.engaged = "X"
r.run(60, live_pass(300, 10))
check("a long move the long way round is never called stalled, though its error GROWS first (300 deg, 48 s)",
      r.f.status()["state"] == "following" and abs(r.model.az - 300.0) <= 1.0, str(r.f.status()))

# ---- unreachable -------------------------------------------------------------------
r = Rig(reachable=False)
out = r.f.arm()
check("an unreachable rotator cannot be armed, and the reason is a sentence", out.get("ok") is False and "not answering" in out["error"], str(out))
r.run(3)
st = r.f.status()
check("and the status says unreachable, with no position invented", st["state"] == "unreachable" and "az" not in st, str(st))

r = Rig(az=0, el=0)
r.f.arm(); r.engaged = "X"
r.run(3, live_pass(90, 10))
r.reachable = False
r.run(30, live_pass(90, 10))
check("losing the link is reported as unreachable, not as a stall", r.f.status()["state"] == "unreachable" and r.f.status()["armed"])
r.reachable = True
r.run(30, live_pass(90, 10))
check("and when it comes back it carries on to the bird",
      r.f.status()["state"] == "following" and abs(r.model.az - 90.0) <= 1.0, str(r.f.status()))

# ---- the rotator says no ------------------------------------------------------------
r = Rig(az=0, el=0)
r.model.az_max = 80.0                    # a mount that cannot reach 90 degrees of azimuth
r.f.arm(); r.engaged = "X"
r.run(2, live_pass(90, 20.0))
st = r.f.status()
check("a refused move is reported as refused, with the code", st["state"] == "refused" and "RPRT -1" in st["detail"], str(st))

# ---- elevation limits and the mount's travel -------------------------------------------
class LimRig(Rig):
    def __init__(self, limits, **kw):
        super().__init__(**kw)
        self.limits = limits
        link = RO.RotctlLink(lambda: "sim:4533", transact=self._transact)
        self.f = RO.RotorFollower(link, lambda sat: self.live, lambda: self.engaged,
                                  lambda text, cls="": self.log.append((text, cls)),
                                  limits_fn=lambda: self.limits)


def pass_with(track, az, el, aos="2026-11-23T03:00:00Z"):
    return {"state": "live", "pass": {"aos_az": track[0][0], "aos": aos, "track": track},
            "geometry": {"az": az, "el": el}}


SOUTH = [(120.0, 5.0), (150.0, 30.0), (180.0, 45.0), (210.0, 30.0), (240.0, 5.0)]      # never near north
NORTH = [(330.0, 5.0), (350.0, 20.0), (10.0, 30.0), (30.0, 20.0), (50.0, 5.0)]         # through north

check("a track through 350 -> 10 crosses north", RO.crosses_north(NORTH) is True)
check("a track through south does not", RO.crosses_north(SOUTH) is False)
check("an empty track does not", RO.crosses_north([]) is False)

r = LimRig({"el_min": 10.0, "el_max": 90.0}, az=100, el=20)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(SOUTH, 150.0, 4.0))
check("a lower limit of 10 holds elevation at 10 for a bird at 4", r.moves() == ["P 150.0 10.0"], str(r.moves()))
r.run(1, pass_with(SOUTH, 160.0, 25.0))
check("above the limit it follows the bird", r.moves()[-1] == "P 160.0 25.0", str(r.moves()))
st = r.f.status()
check("the status carries the limits for the page", (st["el_min"], st["el_max"]) == (10.0, 90.0), str(st))

r = LimRig({"el_min": 10.0, "el_max": 90.0}, az=100, el=20)
r.f.arm(); r.engaged = "X"
r.run(1, {"state": "next", "pass": {"aos_az": 120.0, "aos": "t", "track": SOUTH}, "seconds_to_aos": 100})
check("waiting at the AOS bearing, it waits at the lower limit, not at the horizon", r.moves() == ["P 120.0 10.0"], str(r.moves()))

r = LimRig({"el_max": 90.0}, az=300, el=0)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(NORTH, 350.0, 20.0))
st = r.f.status()
check("a 0-90 mount is sent the plain bearing even on a north-crossing pass", r.moves() == ["P 350.0 20.0"], str(r.moves()))
check("and the operator is told to expect the swing", "crosses north: long swing" in st["detail"] and st["flipped"] is False, str(st))
check("in the log too, once", sum("cannot flip" in t for t, _ in r.log) == 1, str(r.log))

r = LimRig({"el_max": 180.0}, az=150, el=0)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(NORTH, 350.0, 20.0))
check("a 0-180 mount works a north-crossing pass flipped: azimuth + 180, elevation 180 - el",
      r.moves() == ["P 170.0 160.0"], str(r.moves()))
r.run(1, pass_with(NORTH, 10.0, 30.0))
check("so crossing north becomes crossing south, with no jump in azimuth (170 -> 190)",
      r.moves()[-1] == "P 190.0 150.0", str(r.moves()))
check("the status says it is flipped", r.f.status()["flipped"] is True and "(flipped)" in r.f.status()["detail"])
r.limits = {"el_max": 90.0}
r.run(1, pass_with(NORTH, 30.0, 20.0))
check("the choice is fixed for the pass: changing the setting mid-pass does not un-flip it",
      r.moves()[-1] == "P 210.0 160.0", str(r.moves()))

r = LimRig({"el_max": 180.0}, az=100, el=0)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(SOUTH, 150.0, 30.0))
check("a 0-180 mount does NOT flip a pass that stays clear of north", r.moves() == ["P 150.0 30.0"]
      and r.f.status()["flipped"] is False, str(r.moves()))

r = LimRig({"el_min": 10.0, "el_max": 180.0}, az=150, el=170)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(NORTH, 340.0, 3.0))
check("flipped, the lower limit still applies: 10 above the far horizon is 170", r.moves() == ["P 160.0 170.0"], str(r.moves()))

r = LimRig({"el_max": 180.0}, az=150, el=160)
r.f.arm(); r.engaged = "X"
r.run(1, pass_with(NORTH, 350.0, 20.0, aos="A"))
r.run(1, pass_with(SOUTH, 150.0, 30.0, aos="B"))
check("the next pass is decided afresh", r.moves()[-1] == "P 150.0 30.0" and r.f.status()["flipped"] is False, str(r.moves()))

for bad, want in (({"el_min": "x", "el_max": None}, (0.0, 90.0)), ({"el_min": -5, "el_max": 135}, (0.0, 90.0)),
                  ({"el_min": 80, "el_max": 200}, (45.0, 180.0)), ({}, (0.0, 90.0))):
    r = LimRig(bad)
    check(f"limits {bad} are read as {want}", r.f.limits() == want, str(r.f.limits()))

# ---- no rotator set up ---------------------------------------------------------------
f = RO.RotorFollower(RO.RotctlLink(lambda: ""), lambda s: {}, lambda: None)
f.step(1.0)
check("with no rotator set up it is 'off' and not configured", f.status()["state"] == "off" and not f.status()["configured"])
check("and cannot be armed", f.arm().get("ok") is False)

# ---- a fresh follower is never armed ---------------------------------------------------
r2 = Rig(az=5, el=5)
check("a new follower (a restart) starts unarmed", r2.f.armed is False and r2.f.status()["armed"] is False)

# ---- over a real socket, against the simulator's rotctld port ---------------------------
srv = SIM.Server(("127.0.0.1", 0), SIM.RotorModel(az=77, el=12), handler=SIM.RotctlHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
target = "127.0.0.1:%d" % srv.server_address[1]
link = RO.RotctlLink(lambda: target)
pos = link.read()
check("over TCP: read gives the position", pos.get("reachable") and (pos["az"], pos["el"]) == (77.0, 12.0), str(pos))
check("over TCP: a move is accepted", link.set(80.0, 12.0) is True)
check("over TCP: an impossible move is refused with a sentence", "refused" in str(link.set(80.0, 500.0)))
check("over TCP: stop is acknowledged", link.stop() is True)
srv.shutdown(); srv.server_close()
dead = socket.socket(); dead.bind(("127.0.0.1", 0)); port = dead.getsockname()[1]; dead.close()
pos = RO.RotctlLink(lambda: "127.0.0.1:%d" % port).read()
check("over TCP: nothing listening is unreachable, not an exception", pos == {"configured": True, "reachable": False,
                                                                           "target": "127.0.0.1:%d" % port}, str(pos))

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
