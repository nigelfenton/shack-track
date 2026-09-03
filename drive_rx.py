#!/usr/bin/env python3
"""drive_rx.py -- track a satellite downlink while LEAVING THE OPERATOR IN CHARGE.

A fallback for when no independent tracker (Gpredict) is driving the radio.
It is NOT the product: the operating view is meant to observe, and when this
script drives, "matches downlink" only proves the radio obeys the number it
was sent. Use Gpredict when you can; use this to at least hear the bird.

THE OFFSET RULE (Nigel, 2026-09-02, RS-44 pass). A linear transponder is 60 kHz
wide and the interesting thing is a VOICE somewhere inside it, not the band
centre. The first version recentred on the nominal downlink every second, so
tuning onto a signal was undone a second later -- "some voice but the retune
kept moving off freq!". The Doppler was right and the radio was useless.

So the operator's offset from the Doppler-corrected centre is STATE, not error.
Each tick: read the radio, subtract the centre we last commanded, and if the
difference exceeds --detent the operator moved -- adopt it as the new offset and
keep it. Every command after that is centre + offset, so Doppler still tracks
and the signal stays put.

    tuned = radio - centre_we_sent          # what the operator did
    next  = new_centre + tuned              # keep it

Anything below --detent is our own rounding or the radio's step size and is
ignored. --offset seeds an offset before AOS.

RECEIVE ONLY, and refuses to act if the radio reports PTT on:
  - rigctl `t`  (get_ptt) must answer 0 before every write
  - rigctl `M`  once, to put the receive mode on (default USB, 2400 Hz)
  - rigctl `f`  every tick, to see where the operator has tuned
  - rigctl `F`  at most once a second, and only when the target actually moved

    python drive_rx.py --live http://10.0.0.51:8781/api/live --sat RS-44
    python drive_rx.py --live ... --sat RS-44 --offset -5000   # start 5 kHz low
    python drive_rx.py --live ... --sat RS-44 --dry-run

Stops on its own at LOS (+30 s) or on Ctrl-C. While the live feed names a
different bird it waits and drives nothing, so it never follows someone else's
pass by accident.
"""

import argparse
import json
import socket
import sys
import time
import urllib.request


def rigctl(host, port, cmd, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(cmd + b"\n")
            return s.recv(200).decode("utf-8", "replace").strip()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", required=True, help="Shack-Track /api/live URL")
    ap.add_argument("--sat", required=True, help="only drive while this bird is the live/next pass")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4532)
    ap.add_argument("--mode", default="USB")
    ap.add_argument("--passband", type=int, default=2400)
    ap.add_argument("--step", type=int, default=20, help="minimum change (Hz) before a new F is sent")
    ap.add_argument("--lead", type=int, default=120, help="start driving this many seconds before AOS")
    ap.add_argument("--offset", type=int, default=0,
                    help="start with this offset (Hz) from the transponder centre")
    ap.add_argument("--detent", type=int, default=150,
                    help="a radio-vs-commanded difference this big (Hz) is the OPERATOR tuning "
                         "and becomes the new offset; smaller is rounding and is ignored")
    ap.add_argument("--max-offset", type=int, default=40000,
                    help="refuse to adopt an offset bigger than this (Hz): past the passband "
                         "the operator has left this bird, and following would be nonsense")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if rigctl(a.host, a.port, b"f") is None:
        print(f"radio not reachable on {a.host}:{a.port}", file=sys.stderr)
        return 2
    mode_set = False
    last_sent = None          # the frequency we last commanded (centre + offset)
    offset = a.offset         # the operator's offset from centre, in Hz -- KEPT
    print(f"driving RX for {a.sat} from {a.live}  (dry-run={a.dry_run}, "
          f"offset {offset:+d} Hz, detent {a.detent} Hz)")
    while True:
        try:
            with urllib.request.urlopen(a.live, timeout=6) as r:
                d = json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"live feed error: {e}"); time.sleep(2); continue
        p = d.get("pass")
        if not p or p.get("sat") != a.sat:
            # Another bird is live or next (AO-123 rises 24 min before RS-44
            # tonight). Wait for ours to become the feed's pass; do not drive
            # anyone else's and do not give up.
            print(f"{time.strftime('%H:%M:%S')} feed is on {p and p.get('sat')}, waiting for {a.sat}", flush=True)
            time.sleep(5); continue
        if d["state"] == "next" and d.get("seconds_to_aos", 1e9) > a.lead:
            print(f"AOS in {d['seconds_to_aos']} s, waiting", end="\r"); time.sleep(5); continue
        if d["state"] == "live" and d.get("seconds_to_los", 0) < -30:
            print("LOS passed: stopping"); return 0
        dn = d.get("downlink")
        if not dn:
            print("no downlink in feed"); return 1
        ptt = rigctl(a.host, a.port, b"t")
        if ptt is None or not ptt.startswith("0"):
            print(f"PTT reads {ptt!r}: NOT touching the radio"); time.sleep(1); continue
        if not mode_set:
            cmd = f"M {a.mode} {a.passband}".encode()
            print("->", cmd.decode(), "" if a.dry_run else rigctl(a.host, a.port, cmd)); mode_set = True
        centre = dn["hz"]

        # --- the offset rule -------------------------------------------------
        # Where is the radio NOW versus what we last told it? Anything past the
        # detent is the operator's hand on the dial, and it is theirs to keep.
        if last_sent is not None and not a.dry_run:
            cur = rigctl(a.host, a.port, b"f")
            try:
                cur_hz = int(cur.split()[0]) if cur else None
            except (ValueError, IndexError):
                cur_hz = None
            if cur_hz is not None:
                moved = cur_hz - last_sent
                if abs(moved) >= a.detent:
                    new_offset = offset + moved
                    if abs(new_offset) <= a.max_offset:
                        offset = new_offset
                        print(f"{time.strftime('%H:%M:%S')} operator tuned {moved:+d} Hz "
                              f"-> keeping offset {offset:+d} Hz from centre", flush=True)
                    else:
                        print(f"{time.strftime('%H:%M:%S')} operator tuned {moved:+d} Hz "
                              f"({new_offset:+d} from centre, past --max-offset {a.max_offset}): "
                              f"NOT following, and not steering back either", flush=True)
                        time.sleep(1)
                        continue

        hz = centre + offset
        if last_sent is None or abs(hz - last_sent) >= a.step:
            reply = "" if a.dry_run else rigctl(a.host, a.port, f"F {hz}".encode())
            last_sent = hz
            g = d.get("geometry", {})
            off = f" [offset {offset:+d}]" if offset else ""
            print(f"{time.strftime('%H:%M:%S')} F {hz}{off}  ({dn['shift_hz']:+d} Hz, "
                  f"el {g.get('el')}, rr {g.get('range_rate_kms')} km/s) {reply}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
