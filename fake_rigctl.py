#!/usr/bin/env python3
"""fake_rigctl.py -- a stand-in rigctl server for exercising the radio row.

Answers only `f` (frequency) and `m` (mode), the two verbs server.py sends.
Everything else gets RPRT -1. Use it to see the operating view's Radio row in
its REACHABLE state without a radio, and to check the "matches / off downlink"
judgement: pass --track to answer with the current Doppler-corrected downlink
from /api/live (so the row should read "matches"), or --hz for a fixed value
(so it should read "off").

    python fake_rigctl.py --port 4599 --hz 145800000
    python fake_rigctl.py --port 4599 --track http://127.0.0.1:8781/api/live

Binds 127.0.0.1 only. This is a test fixture for Shack-Track's own client; it
is not a radio and must never be pointed at by anything that keys one.
"""

import argparse
import json
import socketserver
import urllib.request


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode("utf-8", "replace").strip()
        cmd = line.split()[0] if line else ""
        if cmd == "f":
            self.wfile.write(f"{self.server.freq()}\n".encode())
        elif cmd == "m":
            self.wfile.write(f"{self.server.mode}\n2400\n".encode())
        else:
            self.wfile.write(b"RPRT -1\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, addr, hz, mode, track):
        super().__init__(addr, Handler)
        self.hz, self.mode, self.track = hz, mode, track

    def freq(self):
        if self.track:
            try:
                with urllib.request.urlopen(self.track, timeout=0.5) as r:
                    d = json.load(r)
                if d.get("downlink"):
                    return d["downlink"]["hz"]
            except Exception:  # noqa: BLE001 -- fall back to the fixed value
                pass
        return self.hz


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4599)
    ap.add_argument("--hz", type=int, default=145800000)
    ap.add_argument("--mode", default="FM")
    ap.add_argument("--track", default=None, help="URL of /api/live to follow the downlink")
    a = ap.parse_args()
    with Server(("127.0.0.1", a.port), a.hz, a.mode, a.track) as srv:
        print(f"fake rigctl on 127.0.0.1:{a.port}  f={a.hz} m={a.mode} track={a.track or '-'}")
        srv.serve_forever()
