#!/usr/bin/env python3
"""test_receiver.py -- the second, receive-only radio, with no radio at all.

The station can have a "receiver" beside the transceiver: an RTL-SDR behind
AetherSDR or SDR++. An engagement drives one or the other. What must hold:

  * a bird listed only with a receive-only frequency (`rx_khz`, the Meteor
    weather birds on 137 MHz) can be tracked on the receiver, and the
    TRANSCEIVER IS NEVER SENT ANYTHING for it -- not on the receiver's
    engagement, and not if someone engages it on the radio either
  * an SDR program that does not implement PTT (answers RPRT) is still
    tracked on the receiver; the same answer from the transceiver holds
  * the choice survives a restart, and an old state file means "radio"
  * the receiver cannot be given the radio's own address

    python test_receiver.py
"""

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tracker as T

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


class FakeRadio:
    def __init__(self, hz=100_000_000, ptt="0"):
        self.hz, self.ptt = hz, ptt
        self.seen, self.writes, self.modes = [], [], []
        self.lock = threading.Lock()

    def __call__(self, cmd: bytes):
        with self.lock:
            c = cmd.decode()
            self.seen.append(c)
            if c == "f":
                return f"{self.hz}"
            if c == "t":
                return self.ptt
            if c.startswith("F "):
                self.hz = int(c.split()[1]); self.writes.append(self.hz); return "RPRT 0"
            if c.startswith("M "):
                self.modes.append(c[2:]); return "RPRT 0"
            return "RPRT -1"


T.IDLE_TICK_S = 0.3
T.TICK_S = 0.15

METEOR = {"state": "live", "seconds_to_aos": -30, "seconds_to_los": 600,
          "pass": {"sat": "METEOR-M2-4", "display": "Meteor M2-4",
                   "aos": "2026-11-16T19:52:09Z", "peak_el": 47},
          "downlink": None,
          "rx": {"hz": 137_902_500, "shift_hz": 2_500, "rest_hz": 137_900_000},
          "geometry": {"el": 30.0}}
HAM = {"state": "live", "seconds_to_aos": -30, "seconds_to_los": 600,
       "pass": {"sat": "RS-44", "display": "RS-44", "aos": "2026-11-16T05:55:00Z",
                "peak_el": 60, "down_mode": "USB"},
       "downlink": {"hz": 435_640_000, "shift_hz": 0},
       "rx": {"hz": 435_640_000, "shift_hz": 0}, "geometry": {"el": 30.0}}


def tmpstate():
    return Path(tempfile.mkdtemp()) / "tracker-state.json"


def run(feed, target, radio, receiver, secs=0.9):
    t = T.Tracker(tmpstate(), lambda sat: feed, radio, receiver)
    out = t.engage(feed["pass"]["sat"], target=target)
    time.sleep(secs)
    st = t.status()
    t.disengage()
    return out, st


# --- 1. a weather bird on the receiver ---------------------------------------
radio, rx = FakeRadio(435_000_000), FakeRadio()
out, st = run(METEOR, "receiver", radio, rx)
check("Meteor engages on the receiver", out.get("ok") and st["state"] == "tracking", str(st))
check("the receiver is tuned to the Doppler-corrected rx frequency",
      rx.writes and rx.writes[-1] == 137_902_500, str(rx.writes[-3:]))
check("the transceiver saw NOTHING, not even a read", radio.seen == [], str(radio.seen[:5]))
check("mode is left alone when the bird names none", rx.modes == [], str(rx.modes))
check("status names the target", st["target"] == "receiver")

# --- 2. the same bird engaged on the transceiver ------------------------------
radio, rx = FakeRadio(435_000_000), FakeRadio()
out, st = run(METEOR, "radio", radio, rx)
check("on the radio the Meteor only waits", st["state"] == "waiting", str(st))
check("and the radio is never written to", radio.writes == [] and radio.modes == [],
      f"F {radio.writes} M {radio.modes}")
check("and the receiver is left alone too", rx.seen == [], str(rx.seen[:5]))

# --- 3. an ordinary ham bird on the receiver ----------------------------------
radio, rx = FakeRadio(435_000_000), FakeRadio()
out, st = run(HAM, "receiver", radio, rx)
check("a ham bird can be heard on the receiver", st["state"] == "tracking"
      and rx.writes and rx.writes[-1] == 435_640_000, str(st))
check("with the bird's own mode", rx.modes == ["USB 2400"], str(rx.modes))
check("and the transceiver untouched", radio.seen == [], str(radio.seen[:5]))

# --- 4. PTT: RPRT is acceptable from an SDR, never from the transceiver -------
radio, rx = FakeRadio(), FakeRadio(ptt="RPRT -11")
out, st = run(HAM, "receiver", radio, rx)
check("receiver that cannot answer PTT is still tracked", st["state"] == "tracking" and rx.writes,
      str(st))
radio = FakeRadio(435_000_000, ptt="RPRT -11")
out, st = run(HAM, "radio", radio, FakeRadio())
check("transceiver that cannot answer PTT is HELD", st["state"] == "holding" and radio.writes == [],
      f"{st['state']} writes {radio.writes}")
rx = FakeRadio(ptt="1")
out, st = run(HAM, "receiver", FakeRadio(), rx)
check("a receiver reporting PTT on is held too", st["state"] == "holding" and rx.writes == [],
      f"{st['state']} writes {rx.writes}")

# --- 5. no receiver, wrong target, one engagement ------------------------------
t = T.Tracker(tmpstate(), lambda sat: HAM, FakeRadio())
out = t.engage("RS-44", target="receiver")
check("engaging the receiver with none set up is refused",
      out.get("ok") is False and not t.status()["engaged"], str(out))
out = t.engage("RS-44", target="antenna")
check("an unknown target is refused", out.get("ok") is False, str(out))
t = T.Tracker(tmpstate(), lambda sat: HAM, FakeRadio(435_640_000), FakeRadio())
t.engage("RS-44", target="radio")
out = t.engage("RS-44", target="receiver")
check("the same bird cannot be engaged on both at once",
      out.get("ok") is False and t.status()["target"] == "radio", str(out))
t.disengage()

# --- 6. restart ----------------------------------------------------------------
sf = tmpstate()
rx = FakeRadio()
t = T.Tracker(sf, lambda sat: METEOR, FakeRadio(), rx)
t.engage("METEOR-M2-4", target="receiver")
time.sleep(0.5)
saved = json.loads(sf.read_text(encoding="utf-8"))
check("the target is saved with the engagement", saved.get("target") == "receiver", str(saved))
t._stop.set()                                   # the server dies; the file stays
radio2, rx2 = FakeRadio(435_000_000), FakeRadio()
t2 = T.Tracker(sf, lambda sat: METEOR, radio2, rx2)
t2.load()
time.sleep(0.8)
check("after a restart it is back on the RECEIVER",
      t2.status()["target"] == "receiver" and rx2.writes and radio2.seen == [],
      f"{t2.status()['target']} rx {rx2.writes[-1:]} radio saw {radio2.seen[:3]}")
t2.disengage()
sf = tmpstate()
sf.write_text(json.dumps({"sat": "RS-44", "offset": 0}), encoding="utf-8")   # pre-receiver file
radio3 = FakeRadio(435_640_000)
t3 = T.Tracker(sf, lambda sat: HAM, radio3, FakeRadio())
t3.load()
time.sleep(0.6)
check("a state file from before receivers means the radio", t3.status()["target"] == "radio"
      and radio3.writes, str(t3.status()))
t3.disengage()

# --- 7. the server: setup, engage, live ---------------------------------------
tmp = Path(tempfile.mkdtemp())
SATS = tmp / "satellites.json"
shutil.copy(HERE / "satellites.json", SATS)
os.environ.update(SHACKTRACK_SATS=str(SATS), SHACKTRACK_STATE=str(tmp / "ts.json"),
                  SHACKTRACK_RIGCTL="127.0.0.1:1", SHACKTRACK_KEPS="",
                  SHACKTRACK_WX_TLE=str(HERE / "test-fixtures" / "weather-2026-11-16.tle"))
os.environ.pop("SHACKTRACK_RECEIVER", None)
import server  # noqa: E402
import passes as engine  # noqa: E402


class FakeRig(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().strip()
        self.wfile.write(b"137900000\n" if line == b"f" else b"WFM\n150000\n" if line == b"m"
                         else b"RPRT -1\n")


rig = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeRig)
threading.Thread(target=rig.serve_forever, daemon=True).start()
RX = "127.0.0.1:%d" % rig.server_address[1]
c = server.app.test_client()

check("no receiver until one is saved", c.get("/api/setup").get_json()["receiver"] == {"target": ""})
r = c.post("/api/track", json={"engage": True, "sat": "RS-44", "target": "receiver"})
check("engaging the receiver before one is set up is a 409 with a sentence",
      r.status_code == 409 and "setup" in r.get_json()["error"], str(r.get_json()))
r = c.post("/api/track", json={"engage": True, "sat": "RS-44", "target": "both"})
check("a made-up target is a 400", r.status_code == 400)

r = c.post("/api/setup", json={"receiver": "not an address"})
check("a malformed receiver address is refused, naming the receiver",
      r.status_code == 400 and "receiver" in r.get_json()["error"], str(r.get_json()))
r = c.post("/api/setup", json={"receiver": "127.0.0.1:1"})
check("the receiver cannot be the radio's address", r.status_code == 400
      and "same address" in r.get_json()["error"], str(r.get_json()))
r = c.post("/api/setup", json={"rigctl": RX, "receiver": RX})
check("nor the address being saved for the radio in the same form", r.status_code == 400)
check("a refused save wrote nothing",
      "receiver" not in json.loads(SATS.read_text(encoding="utf-8")))

r = c.post("/api/setup", json={"receiver": RX})
check("a receiver address saves", r.status_code == 200
      and r.get_json()["receiver"] == {"target": RX}, str(r.get_json().get("receiver")))
check("and the radio's address is unchanged",
      r.get_json()["rigctl"]["target"] == "127.0.0.1:1")
live = c.get("/api/live").get_json()
x = live.get("receiver", {})
check("the operating view reads the receiver",
      x.get("configured") and x.get("reachable") and x.get("hz") == 137_900_000
      and x.get("mode") == "WFM", str(x))
check("and still reports the radio separately", live["radio"]["reachable"] is False)

r = c.post("/api/setup", json={"receiver": ""})
check("clearing the receiver removes it",
      r.status_code == 200 and "receiver" not in json.loads(SATS.read_text(encoding="utf-8")))
check("then the view says there is none",
      c.get("/api/live").get_json()["receiver"] == {"configured": False}
      or time.sleep(1.0) or c.get("/api/live").get_json()["receiver"] == {"configured": False})

# --- 8. the engine: rx for a Meteor, and never a downlink ----------------------
cfg = engine.load_config(HERE / "satellites.json")
cfg["qth"] = {"name": "FM18SJ", "lat": 38.3758, "lon": -76.4154, "alt_m": 19}
T0 = datetime(2026, 11, 16, 18, 0, tzinfo=timezone.utc)
ls = engine.live_state(cfg, HERE / "amateur.tle", now=T0, sat="METEOR-M2-4")
check("a Meteor has an rx frequency near 137.9 MHz",
      ls.get("rx") and abs(ls["rx"]["hz"] - 137_900_000) < 4_000
      and ls["rx"]["rest_hz"] == 137_900_000, str(ls.get("rx")))
check("and NO downlink, so the transceiver has nothing to tune to", ls.get("downlink") is None,
      str(ls.get("downlink")))
ls = engine.live_state(cfg, HERE / "amateur.tle", now=T0, sat="RS-44")
check("an ordinary bird's rx is simply its downlink",
      ls.get("downlink") and ls["rx"] == ls["downlink"], f"{ls.get('rx')} vs {ls.get('downlink')}")

rig.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
