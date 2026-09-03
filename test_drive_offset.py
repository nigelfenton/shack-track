#!/usr/bin/env python3
"""test_drive_offset.py -- prove drive_rx keeps the operator's tuning offset.

No radio and no network: a fake rigctl server stands in for the IC-9700, and a
fake /api/live serves a downlink that sweeps like a real pass. Then we tune the
fake radio mid-pass, exactly as Nigel did on RS-44, and assert that the driver
ADOPTS the offset instead of dragging the radio back to centre.

    python test_drive_offset.py

Written because the first version failed live: "some voice but the retune kept
moving off freq!" -- the bug was invisible to every other test here, because
every other test drives the radio and nobody else touches it.
"""

import json
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

CENTRE_START = 435_645_000     # where the fake pass begins
DRIFT_PER_S = -30              # Hz/s, a receding bird
TUNE_AT_S = 6                  # when the "operator" grabs the dial
TUNE_BY = -4_000               # 4 kHz down, onto an imaginary voice

state = {"hz": CENTRE_START, "t0": time.time(), "sets": []}
fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def centre_now():
    return int(CENTRE_START + DRIFT_PER_S * (time.time() - state["t0"]))


class Rig(socketserver.StreamRequestHandler):
    """Minimal rigctl: f, F, m, M, t."""

    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip()
            if cmd == "f":
                self.wfile.write(f"{state['hz']}\n".encode())
            elif cmd.startswith("F "):
                hz = int(cmd.split()[1])
                state["hz"] = hz
                state["sets"].append((time.time() - state["t0"], hz))
                self.wfile.write(b"RPRT 0\n")
            elif cmd == "m":
                self.wfile.write(b"USB\n2400\n")
            elif cmd.startswith("M "):
                self.wfile.write(b"RPRT 0\n")
            elif cmd == "t":
                self.wfile.write(b"0\n")
            else:
                self.wfile.write(b"RPRT -1\n")
            self.wfile.flush()


class Live(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "state": "live",
            "pass": {"sat": "TEST-1", "display": "TEST-1", "mode": "USB"},
            "seconds_to_aos": -60, "seconds_to_los": 600,
            "downlink": {"hz": centre_now(), "rest_hz": CENTRE_START, "shift_hz": 0},
            "geometry": {"el": 30.0, "az": 180.0, "range_rate_kms": 1.0},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


rig_port, live_port = free_port(), free_port()
rig = socketserver.ThreadingTCPServer(("127.0.0.1", rig_port), Rig)
rig.allow_reuse_address = True
live = HTTPServer(("127.0.0.1", live_port), Live)
threading.Thread(target=rig.serve_forever, daemon=True).start()
threading.Thread(target=live.serve_forever, daemon=True).start()

proc = subprocess.Popen(
    [sys.executable, "-u", str(HERE / "drive_rx.py"),
     "--live", f"http://127.0.0.1:{live_port}/api/live",
     "--sat", "TEST-1", "--host", "127.0.0.1", "--port", str(rig_port),
     "--detent", "150", "--step", "20"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

log = []
threading.Thread(target=lambda: [log.append(l) for l in proc.stdout], daemon=True).start()

try:
    time.sleep(TUNE_AT_S)
    # --- the operator grabs the dial ------------------------------------------
    before_centre = centre_now()
    state["hz"] += TUNE_BY
    tuned_to = state["hz"]
    print(f"\n  [operator tunes {TUNE_BY:+d} Hz to {tuned_to}, centre was ~{before_centre}]\n")
    time.sleep(6)

    sets_after = [(t, hz) for t, hz in state["sets"] if t > TUNE_AT_S + 0.5]
    check("driver kept sending after the operator tuned", len(sets_after) >= 2,
          f"{len(sets_after)} sets")

    # The offset must be preserved: every command after the tune should sit
    # about TUNE_BY below the centre at that moment, NOT back at centre.
    errs = [hz - (int(CENTRE_START + DRIFT_PER_S * t) + TUNE_BY) for t, hz in sets_after]
    worst = max(abs(e) for e in errs) if errs else 9e9
    check("commands keep the operator's offset (not recentred)", worst <= 120,
          f"worst deviation from centre{TUNE_BY:+d} is {worst} Hz")

    # And the radio must never have been dragged back toward the old centre.
    pulled_back = [hz for _, hz in sets_after if hz > tuned_to + 2000]
    check("radio was never dragged back to band centre", not pulled_back,
          f"{len(pulled_back)} commands >2 kHz above where the operator tuned")

    # Doppler must still be tracking underneath the offset: the commands should
    # still be drifting down at roughly DRIFT_PER_S.
    if len(sets_after) >= 2:
        (t1, h1), (t2, h2) = sets_after[0], sets_after[-1]
        rate = (h2 - h1) / (t2 - t1) if t2 > t1 else 0
        check("Doppler still tracked under the offset", abs(rate - DRIFT_PER_S) < 12,
              f"{rate:.1f} Hz/s vs expected {DRIFT_PER_S}")

    check("the adoption was announced in the log",
          any("keeping offset" in l for l in log),
          next((l.strip() for l in log if "keeping offset" in l), "no such line"))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    rig.shutdown()
    live.shutdown()

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
