#!/usr/bin/env python3
"""test_tracker.py -- the standing-engagement behaviours, with no radio.

Engage is a toggle, not a per-pass action (Nigel, 2026-09-02), so these are the
cases that only exist BECAUSE of that decision:

  * it keeps the operator's tuning offset (the RS-44 lesson)
  * it goes idle at LOS and stays engaged, ready for the next pass
  * it survives a restart, re-engaging from disk with the offset intact
  * it idles rather than dying when the radio disappears, and recovers
  * it refuses to engage while another driver is stepping the radio
  * it never writes while PTT is on

    python test_tracker.py
"""

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker as T

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class FakeRadio:
    """Stands in for rigctl: remembers a frequency, can be 'tuned' by hand."""

    def __init__(self):
        self.hz = 435_640_000
        self.ptt = "0"
        self.reachable = True
        self.writes = []
        self.lock = threading.Lock()

    def __call__(self, cmd: bytes):
        with self.lock:
            if not self.reachable:
                return None
            c = cmd.decode()
            if c == "f":
                return f"{self.hz}"
            if c == "t":
                return self.ptt
            if c.startswith("F "):
                self.hz = int(c.split()[1]); self.writes.append(self.hz); return "RPRT 0"
            if c.startswith("M "):
                return "RPRT 0"
            return "RPRT -1"


# A live feed we drive by hand: state, downlink, seconds to AOS/LOS.
feed = {"state": "live", "seconds_to_aos": -30, "seconds_to_los": 600,
        "pass": {"sat": "TEST-1", "display": "TEST-1", "aos": "2026-09-03T00:00:00Z", "peak_el": 40},
        "downlink": {"hz": 435_640_000, "shift_hz": 0}, "geometry": {"el": 30.0}}


def live_fn(sat):
    if feed.get("pass") and feed["pass"]["sat"] != sat:
        return {"pass": None}
    return feed


T.IDLE_TICK_S = 0.4      # keep the test quick
T.TICK_S = 0.2

radio = FakeRadio()
state_file = Path(tempfile.mkdtemp()) / "tracker-state.json"
tr = T.Tracker(state_file, live_fn, radio)

# --- 1. engage and track ----------------------------------------------------
tr.engage("TEST-1")
time.sleep(1.0)
check("engaging starts tracking", tr.status()["state"] == "tracking", tr.status()["detail"])
check("radio was commanded to the downlink", radio.writes and abs(radio.writes[-1] - 435_640_000) < 50,
      f"last write {radio.writes[-1] if radio.writes else None}")

# --- 2. the operator tunes; the offset must be kept -------------------------
with radio.lock:
    radio.hz -= 4_000
time.sleep(1.0)
check("operator's offset is adopted", tr.status()["offset_hz"] == -4_000,
      f"offset {tr.status()['offset_hz']}")
feed["downlink"] = {"hz": 435_641_000, "shift_hz": 1000}   # bird moves on
time.sleep(1.0)
check("Doppler tracks UNDER the offset", abs(radio.writes[-1] - (435_641_000 - 4_000)) < 60,
      f"last write {radio.writes[-1]}, wanted ~{435_641_000 - 4_000}")

# --- 3. LOS: idle but STILL ENGAGED ----------------------------------------
feed.update({"state": "next", "seconds_to_aos": 3600, "seconds_to_los": 0})
time.sleep(1.2)
st = tr.status()
check("still engaged after LOS", st["engaged"] and st["sat"] == "TEST-1")
check("state is waiting between passes", st["state"] == "waiting", st["detail"])
n_writes = len(radio.writes)
time.sleep(1.0)
check("no writes to the radio between passes", len(radio.writes) == n_writes,
      f"{len(radio.writes) - n_writes} writes while idle")

# --- 4. the radio disappears; must idle, not die ---------------------------
feed.update({"state": "live", "seconds_to_aos": -30, "seconds_to_los": 600})
time.sleep(0.6)
radio.reachable = False
time.sleep(1.2)
check("idles when the radio goes away", tr.status()["state"] == "idle-no-radio", tr.status()["detail"])
check("still engaged with the radio gone", tr.status()["engaged"])
radio.reachable = True
time.sleep(1.2)
check("recovers when the radio comes back", tr.status()["state"] == "tracking", tr.status()["detail"])

# --- 5. PTT: never write while transmitting --------------------------------
radio.ptt = "1"
time.sleep(0.6)
n_writes = len(radio.writes)
time.sleep(0.8)
check("no writes while PTT is on", len(radio.writes) == n_writes,
      f"{len(radio.writes) - n_writes} writes with PTT on")
radio.ptt = "0"

# --- 6. restart: re-engage from disk with the offset ------------------------
saved = json.loads(state_file.read_text())
check("engagement is persisted", saved.get("sat") == "TEST-1" and saved.get("offset") == -4_000,
      json.dumps(saved))
tr.disengage()
time.sleep(0.4)
tr2 = T.Tracker(state_file, live_fn, radio)
# a disengage clears the file, so simulate the crash case: engaged state on disk
state_file.write_text(json.dumps({"sat": "TEST-1", "offset": -4_000}))
tr2.load()
time.sleep(0.8)
check("restart re-engages the same bird", tr2.status()["sat"] == "TEST-1")
check("restart restores the offset", tr2.status()["offset_hz"] == -4_000,
      f"offset {tr2.status()['offset_hz']}")
tr2.disengage()

# --- 7. one driver only -----------------------------------------------------
class Drifting(FakeRadio):
    """A radio somebody else is already stepping."""
    def __call__(self, cmd):
        if cmd == b"f":
            with self.lock:
                self.hz += 500        # moving without us asking
                return f"{self.hz}"
        return super().__call__(cmd)

tr3 = T.Tracker(Path(tempfile.mkdtemp()) / "s.json", live_fn, Drifting())
out = tr3.engage("TEST-1")
check("refuses to engage against another driver", out.get("ok") is False,
      out.get("error", "engaged anyway"))
check("stays disengaged after the refusal", tr3.status()["engaged"] is False)

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
