#!/usr/bin/env python3
"""tracker.py -- the standing instruction to work a bird, held by the server.

Nigel, 2026-09-02: "engage should be on when pressed until pressed again for
off ... if the sats i want to track could be weather imaging or data imaging of
sorts it could be left unattended but on/off between los and aos would be good".

So Track is NOT a per-pass action. Pressing it says "work this bird", and the
tracker then goes idle at LOS and wakes itself at the next AOS, for as long as
it takes, until pressed again. Unattended overnight capture is a first-class
use, which forces four things a per-pass button would never have needed:

  1. It survives a server restart -- the engagement and the operator's offset
     are written to disk, so a hub reboot does not silently end an overnight
     run. On start the server re-engages whatever was engaged.
  2. It survives the radio going away -- AetherSDR closing mid-pass idles the
     tracker and is logged; it re-establishes when the radio comes back, and
     never quietly stops.
  3. Exactly one driver. Engaging while something else is already stepping the
     radio is refused, not silently raced. Two drivers was the failure mode of
     2026-09-02, twice in one evening.
  4. Everything is logged, because an unattended run has no witness.

The offset rule from drive_rx.py carries over unchanged: the operator's tuning
is STATE, not error. If the radio moves more than DETENT_HZ from what we last
commanded, that is a hand on the dial and we keep it for the rest of the pass.

RECEIVE ONLY. The verbs sent are `t` (read PTT), `f` (read frequency), `M`
(set receive mode, once per pass) and `F` (set frequency). Nothing here can
key a transmitter, and every write is gated on PTT reading 0.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DETENT_HZ = 150        # radio-vs-commanded difference that counts as the operator
MAX_OFFSET_HZ = 40_000  # past this the operator has left the bird; stop following
STEP_HZ = 20           # do not re-send for less than this
TICK_S = 1.0
IDLE_TICK_S = 15.0     # between passes there is nothing to do; look less often
LOG_MAX = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Tracker:
    """One standing engagement. Owns the only thread that writes to the radio."""

    def __init__(self, state_path: Path, live_fn, rigctl_fn, mode: str = "USB",
                 passband: int = 2400):
        self._path = Path(state_path)
        self._live = live_fn          # () -> live_state dict for the engaged sat
        self._rig = rigctl_fn         # (bytes) -> str | None
        self._mode, self._passband = mode, passband

        self._lock = threading.RLock()
        self._log: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self.sat: str | None = None
        self.offset = 0
        self.last_sent: int | None = None
        self.state = "off"            # off | waiting | tracking | idle-no-radio
        self.detail = ""
        self.mode_set_for: str | None = None

    # ---- persistence ------------------------------------------------------
    def load(self) -> None:
        """Re-engage whatever was engaged before a restart."""
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if d.get("sat"):
            self.offset = int(d.get("offset", 0))
            self.logit(f"restart: re-engaging {d['sat']} (offset {self.offset:+d} Hz)")
            self.engage(d["sat"], offset=self.offset, _restoring=True)

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(
                {"sat": self.sat, "offset": self.offset, "saved": _now_iso()}), encoding="utf-8")
        except OSError as e:
            self.logit(f"could not save tracker state: {e}", "bad")

    # ---- log --------------------------------------------------------------
    def logit(self, text: str, cls: str = "") -> None:
        with self._lock:
            self._log.append({"t": _now_iso(), "text": text, "cls": cls})
            if len(self._log) > LOG_MAX:
                del self._log[0]

    def log(self) -> list[dict]:
        with self._lock:
            return list(self._log)

    # ---- engage / disengage ----------------------------------------------
    def engage(self, sat: str, offset: int = 0, _restoring: bool = False) -> dict:
        with self._lock:
            if self.sat and self.sat != sat:
                return {"ok": False, "error": f"already engaged on {self.sat}; disengage first"}
            if self.sat == sat:
                return self.status()
            # One driver only: if the radio is already being stepped by something
            # else, say so rather than racing it.
            other = self._detect_other_driver()
            if other and not _restoring:
                self.logit(f"refused to engage {sat}: {other}", "bad")
                return {"ok": False, "error": other}
            self.sat = sat
            self.offset = offset
            self.last_sent = None
            self.mode_set_for = None
            self.state = "waiting"
            self.detail = "engaged"
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"track-{sat}", daemon=True)
            self._thread.start()
            self._save()
            if not _restoring:
                self.logit(f"ENGAGED on {sat} - will track every pass until disengaged", "good")
            return self.status()

    def disengage(self) -> dict:
        with self._lock:
            was = self.sat
            self.sat = None
            self.state = "off"
            self.detail = ""
            self._stop.set()
        if was:
            self.logit(f"DISENGAGED from {was}", "warn")
        self._save()
        return self.status()

    def _detect_other_driver(self) -> str | None:
        """Is something else already stepping this radio?

        Two samples a second apart: if the frequency moved by more than the
        detent and we are not the one moving it, another driver is live.
        """
        a = self._read_hz()
        if a is None:
            return None          # unreachable is not "another driver"
        time.sleep(1.0)
        b = self._read_hz()
        if b is None or abs(b - a) < DETENT_HZ:
            return None
        return (f"another program is already tuning this radio "
                f"({a} -> {b} Hz in 1 s); disengage it first")

    # ---- radio ------------------------------------------------------------
    def _read_hz(self) -> int | None:
        r = self._rig(b"f")
        try:
            return int(r.split()[0]) if r else None
        except (ValueError, IndexError):
            return None

    # ---- the loop ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            sat = self.sat
            if not sat:
                return
            try:
                delay = self._tick(sat)
            except Exception as e:  # noqa: BLE001 -- a standing instruction must not die
                self.logit(f"tracker error (continuing): {type(e).__name__}: {e}", "bad")
                delay = IDLE_TICK_S
            self._stop.wait(delay)

    def _tick(self, sat: str) -> float:
        d = self._live(sat)
        if not d or d.get("error"):
            self._set_state("idle-no-data", d.get("error") if d else "no live data")
            return IDLE_TICK_S

        p = d.get("pass")
        if not p:
            self._set_state("waiting", f"no pass for {sat} in the next 24 h")
            return IDLE_TICK_S

        # Between passes: nothing to do, but stay engaged. This is the half that
        # makes an unattended overnight run possible.
        if d.get("state") != "live":
            secs = d.get("seconds_to_aos")
            self._set_state("waiting", f"AOS in {secs} s" if secs is not None else "waiting")
            self.last_sent = None          # a new pass starts from centre + offset
            self.mode_set_for = None
            return IDLE_TICK_S if (secs or 0) > 120 else TICK_S

        dn = d.get("downlink")
        if not dn:
            self._set_state("waiting", "this bird has no downlink listed")
            return IDLE_TICK_S

        # PTT gate: never write while the radio says it is transmitting.
        ptt = self._rig(b"t")
        if ptt is None:
            self._set_state("idle-no-radio", "radio not reachable")
            self.last_sent = None
            return TICK_S
        if not ptt.startswith("0"):
            self._set_state("holding", "radio is transmitting; not touching it")
            return TICK_S

        # The offset rule: what the operator did is theirs to keep.
        if self.last_sent is not None:
            cur = self._read_hz()
            if cur is not None:
                moved = cur - self.last_sent
                if abs(moved) >= DETENT_HZ:
                    new_off = self.offset + moved
                    if abs(new_off) <= MAX_OFFSET_HZ:
                        self.offset = new_off
                        self._save()
                        self.logit(f"operator tuned {moved:+d} Hz - keeping offset "
                                   f"{self.offset:+d} Hz from centre")
                    else:
                        self._set_state("holding",
                                        f"operator is {new_off:+d} Hz from centre, past the "
                                        f"passband; not following and not steering back")
                        return TICK_S

        if self.mode_set_for != p.get("aos"):
            self._rig(f"M {self._mode} {self._passband}".encode())
            self.mode_set_for = p.get("aos")
            self.logit(f"{p.get('display', sat)} AOS - tracking "
                       f"(peak {round(p.get('peak_el', 0))} deg)", "good")

        hz = dn["hz"] + self.offset
        if self.last_sent is None or abs(hz - self.last_sent) >= STEP_HZ:
            self._rig(f"F {hz}".encode())
            self.last_sent = hz
        self._set_state("tracking",
                        f"{dn['shift_hz']:+d} Hz Doppler"
                        + (f", offset {self.offset:+d} Hz" if self.offset else ""))
        return TICK_S

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._lock:
            changed = (state != self.state)
            self.state, self.detail = state, detail
        if changed:
            cls = {"tracking": "good", "idle-no-radio": "bad", "holding": "warn"}.get(state, "")
            self.logit(f"{state}: {detail}" if detail else state, cls)

    # ---- status -----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {"ok": True, "engaged": self.sat is not None, "sat": self.sat,
                    "state": self.state, "detail": self.detail,
                    "offset_hz": self.offset, "last_sent_hz": self.last_sent}
