#!/usr/bin/env python3
"""test_capture.py -- play and record on the receiver, with no receiver at all.

At AOS an engagement on the RECEIVER also asks it to start playing and to
record, and at LOS to stop recording. What must hold:

  * the receiver gets \\start then AOS once per pass, and LOS when the pass ends
  * the TRANSCEIVER NEVER gets any of them, on either kind of engagement
  * disengaging mid-pass stops the recording
  * a stop that cannot be delivered is retried, not forgotten
  * a restart mid-pass still stops the recording it did not start itself
  * a receiver that does not know the verbs is still tracked, and the log says so

    python test_capture.py
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tracker as T

fails = []
CAPTURE = ("\\start", "AOS", "LOS")


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class Fake:
    """A rigctl endpoint. `knows` = it understands the capture verbs (SDR++)."""

    def __init__(self, hz=137_900_000, knows=True, ptt="RPRT 1"):
        self.hz, self.knows, self.down, self.ptt = hz, knows, False, ptt   # an SDR has no PTT
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, cmd: bytes):
        with self.lock:
            c = cmd.decode()
            if self.down:
                return None
            self.seen.append(c)
            if c == "f":
                return f"{self.hz}"
            if c == "t":
                return self.ptt
            if c.startswith("F "):
                self.hz = int(c.split()[1]); return "RPRT 0"
            if c == "\\start":
                return None if self.knows else "RPRT -1"   # SDR++ answers nothing
            if c in ("AOS", "LOS"):
                return "RPRT 0" if self.knows else "RPRT -1"
            return "RPRT -1"

    def capture(self):
        return [c for c in self.seen if c in CAPTURE]


T.IDLE_TICK_S = 0.2
T.TICK_S = 0.1


def feed(live=True, aos="2026-11-17T01:29:37Z", down=None):
    return {"state": "live" if live else "next",
            "seconds_to_aos": -30 if live else 5000, "seconds_to_los": 600,
            "pass": {"sat": "METEOR-M2-3", "display": "Meteor M2-3", "aos": aos, "peak_el": 75,
                     "down_mode": "USB" if down else None},
            "downlink": down,
            "rx": {"hz": 137_902_500, "shift_hz": 2_500, "rest_hz": 137_900_000},
            "geometry": {"el": 30.0}}


def tmpstate():
    return Path(tempfile.mkdtemp()) / "tracker-state.json"


def logtext(t):
    return " | ".join(e["text"] for e in t.log())


# --- 1. one pass on the receiver ----------------------------------------------
now = {"d": feed(True)}
radio, rx = Fake(435_000_000), Fake()
t = T.Tracker(tmpstate(), lambda sat: now["d"], radio, rx)
t.engage("METEOR-M2-3", target="receiver")
time.sleep(0.8)
check("at AOS the receiver is told to play, then to record, once",
      rx.capture() == ["\\start", "AOS"], str(rx.capture()))
check("and the engagement remembers a stop is owed", t.capturing is True)
check("recording starts only after the receiver has been tuned to the bird",
      [c for c in rx.seen if c.startswith("F ") or c == "AOS"][:2] == ["F 137902500", "AOS"],
      str([c for c in rx.seen if c.startswith("F ") or c == "AOS"][:3]))
now["d"] = feed(False)                                # the pass ends
time.sleep(0.8)
check("at LOS the receiver is told to stop recording, once",
      rx.capture() == ["\\start", "AOS", "LOS"], str(rx.capture()))
check("and nothing is owed any more", t.capturing is False)
now["d"] = feed(True, aos="2026-11-17T03:12:14Z")     # the next pass of the standing engagement
time.sleep(0.8)
check("the next pass is recorded too",
      rx.capture() == ["\\start", "AOS", "LOS", "\\start", "AOS"], str(rx.capture()))
check("the transceiver saw nothing at all", radio.seen == [], str(radio.seen[:5]))

# --- 2. disengaging mid-pass ----------------------------------------------------
t.disengage()
check("disengaging mid-pass stops the recording", rx.capture()[-1] == "LOS" and t.capturing is False,
      str(rx.capture()))

def slow(sat):                                        # a tick caught mid-flight
    time.sleep(0.4)
    return feed(True)


t = T.Tracker(tmpstate(), slow, Fake(), Fake())
t.engage("METEOR-M2-3", target="receiver")
time.sleep(0.1)
t.disengage()                                         # ...while that tick is still running
time.sleep(0.8)
check("after disengaging the state reads off, never a late 'tracking'",
      t.status()["state"] == "off", str(t.status()))

# --- 3. the transceiver is never sent capture verbs ------------------------------
radio, rx = Fake(435_640_000, ptt="0"), Fake()
d = feed(True, down={"hz": 435_640_000, "shift_hz": 0})
t = T.Tracker(tmpstate(), lambda sat: d, radio, rx)
t.engage("METEOR-M2-3", target="radio")
time.sleep(0.6)
st = t.status()
t.disengage()
check("an engagement on the radio tracks", st["state"] == "tracking", str(st))
check("and the radio is sent no play or record verb, at AOS or at disengage",
      radio.capture() == [] and rx.seen == [], f"radio {radio.capture()} rx {rx.seen[:3]}")

# --- 4. a stop that cannot be delivered is retried --------------------------------
now = {"d": feed(True)}
rx = Fake()
t = T.Tracker(tmpstate(), lambda sat: now["d"], Fake(), rx)
t.engage("METEOR-M2-3", target="receiver")
time.sleep(0.6)
rx.down = True
now["d"] = feed(False)
time.sleep(0.6)
check("with the receiver unreachable at LOS the stop is still owed", t.capturing is True)
rx.down = False
time.sleep(0.6)
check("and it is delivered when the receiver comes back",
      rx.capture()[-1] == "LOS" and t.capturing is False, str(rx.capture()))
t.disengage()

# --- 5. a restart mid-pass -----------------------------------------------------
sf = tmpstate()
now = {"d": feed(True)}
rx = Fake()
t = T.Tracker(sf, lambda sat: now["d"], Fake(), rx)
t.engage("METEOR-M2-3", target="receiver")
time.sleep(0.6)
check("the owed stop is saved with the engagement",
      json.loads(sf.read_text(encoding="utf-8")).get("capturing") is True)
t._stop.set()                                         # the server dies mid-pass
now["d"] = feed(False)                                # ...and the pass ends while it is down
rx2 = Fake()
t2 = T.Tracker(sf, lambda sat: now["d"], Fake(), rx2)
t2.load()
time.sleep(0.6)
check("after the restart the recording it did not start is still stopped",
      rx2.capture() == ["LOS"], str(rx2.capture()))
t2.disengage()

# --- 6. a receiver that does not know the verbs -----------------------------------
rx = Fake(knows=False)
d = feed(True)
t = T.Tracker(tmpstate(), lambda sat: d, Fake(), rx)
t.engage("METEOR-M2-3", target="receiver")
time.sleep(0.6)
st = t.status()
check("a receiver that does not know the verbs is still tracked",
      st["state"] == "tracking" and any(c.startswith("F ") for c in rx.seen), str(st))
check("and the log says it may not be recording", "may not be recording" in logtext(t), logtext(t))
t.disengage()

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
