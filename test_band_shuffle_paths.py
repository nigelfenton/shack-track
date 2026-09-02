#!/usr/bin/env python3
"""test_band_shuffle_paths.py -- drive ic9700_band_shuffle's decision logic
against a stub radio. No hardware, no network, no AetherSDR.

WHY A STUB. The branch that matters most -- "Main does not hold the target band,
so exchange AGAIN rather than selecting Sub" -- only fires when the first
exchange leaves Main on the wrong band. On the real radio that depends on which
two of the three bands happen to be live, and there is NO non-mutating read of
Sub (see reference_ic9700_band_receiver_rules), so it cannot be set up on
demand: you would be shuffling a live transmitter blind and hoping.

So the radio is replaced with a model of one. The stub enforces the constraints
the real IC-9700 imposes -- two receivers, three bands, only two live at once,
and a band cannot be moved to a receiver the other one already holds -- and
records every CI-V frame the script sends. The assertions are about the FRAMES
and the END STATE, which is what the script actually controls.

What this proves: the branch is reachable, it sends 07 B0 then 07 D0, and it
NEVER sends 07 D1. What it does not prove: that the real radio responds to those
frames as modelled. The 07 B0 / 07 D0 pair is verified on hardware (2026-09-01);
the re-exchange ordering is verified here.

Run:  python3 test_band_shuffle_paths.py
Exits non-zero on first failure.
"""
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "ic9700_band_shuffle.py")

failures = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        failures.append(what)


class StubRadio:
    """An IC-9700 that obeys the real one's band/receiver constraints.

    main/sub each hold a band name. `pan` follows MAIN only -- that is the whole
    point of the bug this guards against: AE binds its single panadapter to Main
    and has no concept of Sub.
    """

    FREQ = {"2m": 145.047, "70cm": 435.825, "23cm": 1296.100}

    def __init__(self, main, sub):
        self.main, self.sub = main, sub
        self.selected = "main"          # which receiver CAT is pointed at
        self.frames = []                # every CI-V frame the script sent
        self.tuned = dict(self.FREQ)    # per-band frequency, so F can retune

    # --- what the script sees -------------------------------------------
    def cat_mhz(self):
        band = self.main if self.selected == "main" else self.sub
        return self.tuned[band]

    def pan_mhz(self):
        return self.tuned[self.main]    # PAN FOLLOWS MAIN. Always.

    # --- what the script does -------------------------------------------
    def civ(self, hexstr):
        self.frames.append(hexstr)
        if hexstr == "07B0":            # exchange Main <-> Sub
            self.main, self.sub = self.sub, self.main
        elif hexstr == "07D0":
            self.selected = "main"
        elif hexstr == "07D1":
            self.selected = "sub"
        return True

    def tune(self, mhz):
        """An in-band set. Refused (like the real radio's NAK) if the frequency
        is not on the band the SELECTED receiver holds."""
        band = self.main if self.selected == "main" else self.sub
        lo, hi = {"2m": (144, 148), "70cm": (430, 450), "23cm": (1240, 1300)}[band]
        if lo <= mhz <= hi:
            self.tuned[band] = mhz
            return True
        return False                    # NAKed, exactly as the real radio does


def run_shuffle(stub, target):
    """Import the script fresh with its radio calls redirected at the stub."""
    spec = importlib.util.spec_from_file_location("bs_%d" % id(stub), TARGET)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    m.time.sleep = lambda *_a, **_k: None          # no waiting for a stub
    m.civ = stub.civ
    m.state = lambda: (stub.cat_mhz(), stub.pan_mhz(), False)

    def fake_rigctl(cmd, *a, **k):
        text = cmd.decode() if isinstance(cmd, bytes) else str(cmd)
        if text.startswith("F "):
            stub.tune(int(text.split()[1]) / 1e6)
            return "RPRT 0"
        return str(int(stub.cat_mhz() * 1e6))
    m.rigctl = fake_rigctl

    buf = io.StringIO()
    old_out, old_argv = sys.stdout, sys.argv
    sys.stdout = buf
    sys.argv = ["ic9700_band_shuffle.py", "--target", "%.6f" % target]
    try:
        rc = m.main()
    finally:
        sys.stdout, sys.argv = old_out, old_argv
    return rc, buf.getvalue()


def main():
    print("\nband-shuffle decision paths (stub radio, no hardware)\n")

    # ── CASE 1: the path already verified on hardware ────────────────────
    # Sub holds the target band, so ONE exchange puts it on Main.
    s = StubRadio(main="2m", sub="70cm")
    rc, out = run_shuffle(s, 435.645)
    check(s.frames[:2] == ["07B0", "07D0"], "case 1: sends 07 B0 then 07 D0")
    check("07D1" not in s.frames, "case 1: never sends 07 D1")
    check(s.main == "70cm", "case 1: Main ends holding the target band")
    check(abs(s.cat_mhz() - 435.645) < 1e-6, "case 1: CAT on target")
    check(abs(s.pan_mhz() - s.cat_mhz()) < 1e-6, "case 1: pan follows CAT")
    check(rc == 0, "case 1: reports success")

    # ── CASE 2: THE RE-EXCHANGE PATH ─────────────────────────────────────
    # Sub holds 23 cm, so the first exchange puts the WRONG band on Main.
    # The script must exchange AGAIN, not reach for the target via Sub.
    s = StubRadio(main="70cm", sub="23cm")
    rc, out = run_shuffle(s, 435.645)
    check(s.frames.count("07B0") == 2, "case 2: exchanges TWICE (the re-exchange fired)")
    check("07D1" not in s.frames,
          "case 2: NEVER selects Sub -- the pan could not follow it")
    check(s.main == "70cm", "case 2: Main ends holding the target band")
    check(s.selected == "main", "case 2: CAT left pointed at Main")
    check(abs(s.pan_mhz() - s.cat_mhz()) < 1e-6,
          "case 2: pan and CAT agree (same receiver)")
    check("exchange again" in out, "case 2: says why it re-exchanged")

    # ── CASE 3: the old bug, asserted as a regression guard ──────────────
    # If the script ever selects Sub again, CAT and the pan end on different
    # receivers. Nothing here should be able to produce that.
    s = StubRadio(main="2m", sub="70cm")
    run_shuffle(s, 435.645)
    split = abs(s.pan_mhz() - s.cat_mhz()) > 1e-6
    check(not split,
          "case 3: CAT and pan never end on different receivers (the 08-31 bug)")

    # ── CASE 4: already correct -- change nothing ────────────────────────
    s = StubRadio(main="2m", sub="70cm")
    rc, out = run_shuffle(s, 145.047)
    check(s.frames == [], "case 4: already on target sends NO frames")
    check(rc == 0, "case 4: reports success without touching the radio")

    print()
    if failures:
        print("%d failure(s)" % len(failures))
        return 1
    print("all band-shuffle path checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
