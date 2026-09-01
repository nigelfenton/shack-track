#!/usr/bin/env python3
"""ic9700_band_shuffle.py -- put the IC-9700 on a satellite band BY COMMAND.

The problem this solves: the IC-9700 has two independent receivers (Main and
Sub, each with VFO A/B) and three bands (2 m, 70 cm, 23 cm), but only two bands
can be live at once. A receiver therefore CANNOT be tuned to a band the other
receiver already holds -- the radio answers CI-V `05` (set frequency) with `fa`
(NAK) and stays put.

AetherSDR cannot get round this on its own: its Icom backend never sends `07`
(receiver select) or `1A 01` (band stack), and its model table leaves
`receivers = 1`, so it builds ONE panadapter bound to Main and has no concept of
Sub at all. Worse, rigctl answers `RPRT 0` before the CI-V frame is even sent,
so a NAKed retune still reports success.

The sequence below drives it anyway, using raw CI-V through the automation
bridge:

    07 B0   exchange Main <-> Sub   (moves the band, and the pan follows)
    07 D0   select Main             (points CAT at the receiver that now holds it)

Order matters. Exchanging alone leaves CAT reading the wrong receiver; selecting
alone cannot move a band the other receiver owns.

THE PAN ONLY FOLLOWS MAIN, so both must end there. AE builds one panadapter and
binds it to Main, so `07 D1` (select Sub) produces a correct-looking frequency
with a waterfall still rendering the OTHER receiver -- the display and the
tuned frequency then describe different radios, and nothing says so. This script
therefore never selects Sub: if Main does not hold the target band after the
exchange, it exchanges AGAIN rather than following the band to Sub.

Verified on the real radio 2026-09-01. Going 145.070 -> 435.645, the earlier
version fell back to `07 D1` and ended CAT 435.645 / pan 435.825 -- passing its
own check because the two were within 0.5 MHz, while the waterfall was in fact
showing Main. The final check now compares BANDS, not a megahertz window.

RECEIVE-ONLY INTENT, but note this needs AETHER_AUTOMATION_ALLOW_TX=1 because
raw CI-V *can* key a radio. Nothing here keys: `07` is receiver selection and
`03` is a frequency read. The script refuses to run if the radio reports
transmitting, and re-checks afterwards.

    python ic9700_band_shuffle.py --check          # report state, change nothing
    python ic9700_band_shuffle.py --target 435.645 # shuffle until CAT+pan are there
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time

AE = r"C:\Users\nigel\Documents\AetherSDR"


def probe(*args):
    """Run one automation_probe verb and return the parsed JSON, or None."""
    try:
        out = subprocess.run(
            [sys.executable, "tools/automation_probe.py", *args],
            cwd=AE, capture_output=True, text=True, timeout=60,
        ).stdout
        return json.loads(out)
    except Exception:
        return None


def rigctl(cmd, host="127.0.0.1", port=4532, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(cmd + b"\n")
            return s.recv(200).decode("utf-8", "replace").strip()
    except Exception:
        return None


def state():
    """(cat_mhz, pan_mhz, transmitting) -- what CAT tunes vs what the screen shows."""
    f = rigctl(b"f")
    cat = int(f) / 1e6 if (f and f.lstrip("-").isdigit()) else None
    p = probe("get", "pan")
    pan = p["pan"]["centerMhz"] if p and p.get("ok") else None
    r = probe("get", "radio")
    tx = r["radio"]["transmitting"] if r and r.get("ok") else None
    return cat, pan, tx


def civ(hexstr):
    """Send one raw CI-V frame. Returns True if the bridge accepted it."""
    d = probe("civ", "send", hexstr)
    return bool(d and d.get("ok"))


def on_band(mhz, target_mhz):
    """True if two frequencies are on the SAME IC-9700 band.

    The radio has three: 2 m, 70 cm, 23 cm. Comparing bands rather than a
    kilohertz window is what distinguishes "the pan is a little off" from "the
    pan is bound to the other receiver", which a loose MHz tolerance cannot.
    """
    def band(f):
        if f is None:
            return None
        if 144.0 <= f <= 148.0:
            return "2m"
        if 430.0 <= f <= 450.0:
            return "70cm"
        if 1240.0 <= f <= 1300.0:
            return "23cm"
        return None
    b = band(mhz)
    return b is not None and b == band(target_mhz)


def show(label):
    cat, pan, tx = state()
    print("  %-18s CAT %-11s pan %-11s tx=%s"
          % (label,
             ("%.6f" % cat) if cat else "?",
             ("%.4f" % pan) if pan else "?",
             tx))
    return cat, pan, tx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=435.645,
                    help="frequency the CAT-selected receiver should end on (MHz)")
    ap.add_argument("--check", action="store_true",
                    help="report state and exit without changing anything")
    a = ap.parse_args()

    print("  IC-9700 band shuffle -- target %.6f MHz\n" % a.target)
    cat, pan, tx = show("current:")

    if tx:
        print("\n  ** RADIO IS TRANSMITTING -- refusing to touch it. **")
        return 1
    if cat is None:
        print("\n  ** rigctl not answering on 4532: is AetherSDR running? **")
        return 1

    on_target = cat is not None and abs(cat - a.target) < 0.0005
    pan_ok = pan is not None and abs(pan - a.target) < 0.5
    if on_target and pan_ok:
        print("\n  already correct -- CAT and pan are both on target.")
        return 0
    if a.check:
        print("\n  --check: no change made.")
        return 0

    # 1) Exchange Main <-> Sub. This is what actually moves a BAND between the
    #    two receivers; a plain `05` set cannot, and gets NAKed.
    print("\n  07 B0  exchange Main <-> Sub")
    if not civ("07B0"):
        print("  ** bridge refused the frame (needs AETHER_AUTOMATION_ALLOW_TX=1) **")
        return 1
    time.sleep(2)
    cat, pan, _ = show("after exchange:")

    # 2) Get the target band onto the receiver the PAN is bound to.
    #
    # ⚠ THIS IS THE WHOLE POINT, AND THE FIRST VERSION GOT IT WRONG.
    #
    # AE's Icom backend leaves `receivers = 1`, so it builds ONE panadapter and
    # binds it to MAIN. It has no concept of Sub. So pointing CAT at Sub with
    # `07 D1` "succeeds" -- the frequency reads back correct -- while the
    # waterfall carries on rendering Main. The operator then has a display and
    # a tuned frequency describing DIFFERENT RECEIVERS, and nothing says so.
    #
    # Measured on the real radio 2026-09-01, going 145.070 -> 435.645:
    #     after exchange:  CAT 435.825  pan 435.825   (agree)
    #     after 07 D0:     CAT 435.825  pan 435.825
    #     after 07 D1:     CAT 145.070  pan 435.825   <- split
    #     after tune:      CAT 435.645  pan 435.825   <- pan never followed
    # It passed the old check because |435.645 - 435.825| < 0.5 MHz. The failure
    # was not a 180 kHz offset; it was the pan tracking the other receiver.
    #
    # So: NEVER select Sub to reach the band. If Main does not hold it after the
    # exchange, exchange AGAIN -- that puts the band back on Main, which is the
    # only receiver the pan can follow. Two exchanges return to the start, so a
    # band that cannot be reached on Main cannot be reached at all, and saying
    # so is better than a green result the display contradicts.
    time.sleep(1.5)
    print("\n  07 D0  select Main (the receiver the pan is bound to)")
    civ("07D0")
    time.sleep(2)
    cat, pan, _ = show("after select:")

    if cat is None or not on_band(cat, a.target):
        # Main is not holding the target band. Exchange again so it does,
        # rather than pointing CAT at Sub where the pan cannot follow.
        print("\n  Main does not hold the target band.")
        print("  07 B0  exchange again (NOT 07 D1 -- the pan only follows Main)")
        civ("07B0")
        time.sleep(2)
        civ("07D0")
        time.sleep(2)
        cat, pan, _ = show("after re-exchange:")

    # 3) Fine-tune within the band. This is a normal in-band set, which ACKs.
    if cat is not None and abs(cat - a.target) > 0.0005:
        print("\n  F %d  in-band set" % int(a.target * 1e6))
        rigctl(("F %d" % int(a.target * 1e6)).encode())
        time.sleep(1.5)
        cat, pan, _ = show("after tune:")

    print()
    cat, pan, tx = state()
    ok_cat = cat is not None and abs(cat - a.target) < 0.0005
    # The pan must be on the SAME BAND as CAT, not merely within half a
    # megahertz of the target. A pan bound to the other receiver can sit inside
    # a loose window by coincidence -- that is exactly what hid this bug.
    ok_pan = pan is not None and cat is not None and on_band(pan, cat)
    print("  CAT on target      : %s" % ("YES" if ok_cat else "NO  (%s)" % cat))
    print("  pan follows CAT    : %s%s" % (
        "YES" if ok_pan else "NO",
        "" if ok_pan else "  (pan %s vs CAT %s -- different receivers?)" % (pan, cat)))
    print("  transmitting       : %s" % tx)
    if ok_cat and ok_pan:
        print("\n  ready -- CAT and the pan are on the same receiver.")
        return 0
    if ok_cat and not ok_pan:
        print("\n  NOT ready: the radio is tuned correctly but the WATERFALL IS")
        print("  SHOWING THE OTHER RECEIVER. Do not trust the display.")
        return 2
    print("\n  NOT ready. Re-run, or check which receiver holds which band.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
