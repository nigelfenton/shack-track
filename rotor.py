#!/usr/bin/env python3
"""rotor.py -- point a network rotator at the engaged satellite, and say truthfully where it is.

Speaks Hamlib's rotator network protocol (what `rotctld` serves, by convention
on TCP 4533): `p` to read, `P az el` to move, `S` to stop. That one protocol
reaches a real rotctld in front of any controller, the ESP32 and CSN boxes
that speak it natively, and rotor_sim.py.

Two parts:

  RotctlLink      one round trip per call; never raises; "unreachable" is a
                  value, not an exception, so the page can SAY so.
  RotorFollower   decides when to move. Its rules are the point of this file:

  * It moves NOTHING until armed, and arming is an explicit act on the page.
    It is never restored after a restart: a rotator that starts turning by
    itself when the hub reboots is the failure this rule exists to prevent.
  * Armed, it follows whatever the tracker is engaged on. Nothing engaged,
    nothing moves.
  * During a pass it points at the bird. In the last PREPOSITION_S before AOS
    it goes to the AOS azimuth and waits there. Otherwise it stays still.
  * It re-commands only when the bird has moved THRESHOLD_DEG from the last
    command, so the rotator steps instead of hunting.
  * A command is not an arrival. If the rotator is off target and has not
    MOVED for STALL_S, the follower sends stop, DISARMS and says "stalled". It
    does not keep driving a motor that is not turning. The test is movement,
    not a shrinking error: a 0-360 rotator sent from 0 to 300 goes the long
    way round, and its shortest-path error grows before it falls.
  * At LOS, on disarm, and when the engagement ends, it sends stop.
  * Elevation is held at the station's lower limit (0 unless set higher, for a
    mount that must not point at the horizon) and never sent negative.
  * The mount's elevation travel is a setting: 0-90 or 0-180. With 0-180, a
    pass that crosses north is worked FLIPPED (azimuth + 180, elevation over
    the top), decided once at the start of the pass, so the azimuth rotator
    never has to run through its end stop. With 0-90 such a pass still means a
    long swing, and the status says so in advance.

NOT handled yet: an azimuth rotator with more than 360 degrees of travel
(overlap), an azimuth stop that is not at north, and parking after a pass.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timezone

THRESHOLD_DEG = 2.0       # re-command when the target has moved this far
ON_TARGET_DEG = 3.0       # closer than this counts as pointing at it
STALL_S = 12.0            # off target and not moving for this long = stalled
PROGRESS_DEG = 0.5        # the least change in position that counts as movement
PREPOSITION_S = 240       # go to the AOS azimuth this long before AOS
TICK_S = 1.0
TIMEOUT_S = 0.7


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def az_gap(a: float, b: float) -> float:
    """Shortest angular distance between two azimuths, 0..180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def crosses_north(track) -> bool:
    """True if a pass's azimuth passes through north (359 -> 0 or 0 -> 359).
    `track` is the pass record's list of (az, el) points."""
    az = [float(pt[0]) for pt in track if pt is not None]
    return any(abs(a - b) > 180.0 for a, b in zip(az, az[1:]))


def pointing_error(az: float, el: float, want_az: float, want_el: float) -> float:
    """The larger of the azimuth and elevation errors, in degrees."""
    return max(az_gap(az, want_az), abs(el - want_el))


def parse_position(text: str | None):
    """(az, el) from a reply to `p`, or None. A reply that is an error report,
    short, or not numbers is None: never a guess."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) < 2 or lines[0].startswith("RPRT"):
        return None
    try:
        return float(lines[0]), float(lines[1])
    except ValueError:
        return None


def parse_report(text: str | None):
    """The RPRT code from a reply to a set command, or None if there is none."""
    if not text:
        return None
    for ln in text.replace("\r", "").split("\n"):
        ln = ln.strip()
        if ln.startswith("RPRT"):
            try:
                return int(ln.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def tcp_transact(target: str, cmd: str, timeout: float = TIMEOUT_S) -> str | None:
    """One command, one reply, one connection. None means unreachable."""
    host, _, port = target.rpartition(":")
    want_lines = 2 if cmd.strip() == "p" else 1
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.sendall(cmd.encode("ascii") + b"\n")
            buf = b""
            while buf.count(b"\n") < want_lines and b"RPRT" not in buf:
                chunk = s.recv(200)
                if not chunk:
                    break
                buf += chunk
            if b"RPRT" in buf and not buf.endswith(b"\n"):
                buf += s.recv(50)
            return buf.decode("ascii", "replace")
    except (OSError, ValueError):
        return None


class RotctlLink:
    """The rotator, as three verbs. `transact` is (command text) -> reply text or
    None; the default goes over TCP to whatever `target_fn` names right now."""

    def __init__(self, target_fn, transact=None):
        self._target = target_fn
        self._transact = transact

    def target(self) -> str:
        return self._target() or ""

    def _ask(self, cmd: str) -> str | None:
        if self._transact is not None:
            return self._transact(cmd)
        t = self.target()
        return tcp_transact(t, cmd) if t else None

    def read(self) -> dict:
        t = self.target()
        if not t:
            return {"configured": False, "reachable": False}
        reply = self._ask("p")
        if reply is None:
            return {"configured": True, "reachable": False, "target": t}
        pos = parse_position(reply)
        if pos is None:
            return {"configured": True, "reachable": False, "target": t,
                    "error": f"unexpected reply to p: {reply.strip()[:60]!r}"}
        return {"configured": True, "reachable": True, "target": t,
                "az": round(pos[0], 1), "el": round(pos[1], 1), "read_at": _now_iso()}

    def set(self, az: float, el: float):
        """True if the rotator accepted the target, else a sentence saying why not."""
        reply = self._ask("P %.1f %.1f" % (az % 360.0, max(0.0, el)))
        if reply is None:
            return "the rotator did not answer"
        code = parse_report(reply)
        if code == 0:
            return True
        return f"the rotator refused the move (RPRT {code})" if code is not None \
            else f"unexpected reply to P: {reply.strip()[:60]!r}"

    def stop(self) -> bool:
        return parse_report(self._ask("S")) == 0


class RotorFollower:
    def __init__(self, link: RotctlLink, live_fn, engaged_fn, log_fn=None, limits_fn=None):
        # () -> {"el_min": degrees, "el_max": 90 or 180}; read every step so a
        # change on the setup page takes effect without a restart.
        self._limits = limits_fn or (lambda: {})
        self._flip: tuple[str, bool, bool] | None = None   # (pass key, flipped, crosses north)
        self._link = link
        self._live = live_fn            # (sat) -> live_state dict
        self._engaged = engaged_fn      # () -> engaged satellite name or None
        self._logit = log_fn or (lambda text, cls="": None)
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        self.armed = False
        self.state = "off"              # off | idle | armed | pre-positioning | following
        #                                 | unreachable | refused | stalled
        self.detail = ""
        self.pos: dict = {"configured": False, "reachable": False}
        self.want: tuple[float, float] | None = None
        self.last_cmd: tuple[float, float] | None = None
        self._ref: tuple[float, float] | None = None    # where it was when it last moved
        self._ref_at = 0.0
        self._moving = False            # a target is outstanding; a stop is owed when it ends

    # ---- operator actions -------------------------------------------------
    def arm(self) -> dict:
        with self._lock:
            pos = self._link.read()
            self.pos = pos
            if not pos.get("configured"):
                return {"ok": False, "error": "No rotator is set up. Add one on the setup page."}
            if not pos.get("reachable"):
                return {"ok": False, "error": "The rotator is not answering, so it cannot be armed."}
            if not self.armed:
                self.armed = True
                self._forget_target()
                self._set("armed", "waiting for an engagement")
                self._logit("rotator ARMED - it will follow the engaged satellite", "good")
            return dict(self.status(), ok=True)

    def disarm(self, why: str = "") -> dict:
        with self._lock:
            was = self.armed
            self.armed = False
            stopped = self._link.stop() if (was or self._moving) else True
            self._forget_target()
            self._set("idle", why or "disarmed")
            if was:
                self._logit("rotator disarmed" + (f": {why}" if why else "")
                            + ("" if stopped else " - the stop was NOT acknowledged"),
                            "" if stopped else "bad")
            return dict(self.status(), ok=True, stop_acknowledged=stopped)

    # ---- one decision -----------------------------------------------------
    def step(self, now: float | None = None) -> None:
        """Read the rotator and decide. Called once a second; safe to call by hand."""
        now = time.time() if now is None else now
        with self._lock:
            pos = self._link.read()
            self.pos = pos
            if not pos.get("configured"):
                self.armed = False
                self._forget_target()
                self._set("off", "no rotator set up")
                return
            if not pos.get("reachable"):
                # Stay armed: the operator's instruction stands. But say so, and
                # start the stall clock again when it comes back.
                self._ref = None
                self._set("unreachable", pos.get("error") or "the rotator is not answering")
                return
            if not self.armed:
                if self.state != "stalled":
                    self._set("idle", "not armed")
                return

            sat = self._engaged()
            want, phase, note = (None, "armed", "nothing engaged")
            if sat:
                want, phase, note = self._target_for(self._live(sat) or {}, sat)
            self.want = want
            if want is None:
                self._release()
                self._set("armed", note)
                return

            err = pointing_error(pos["az"], pos["el"], want[0], want[1])
            if self.last_cmd is None or pointing_error(
                    self.last_cmd[0], self.last_cmd[1], want[0], want[1]) >= THRESHOLD_DEG:
                res = self._link.set(want[0], want[1])
                if res is not True:
                    self._set("refused", res)
                    return
                self.last_cmd = want
                self._moving = True
                self._ref, self._ref_at = (pos["az"], pos["el"]), now

            # A command is not an arrival: is the rotator actually moving?
            here = (pos["az"], pos["el"])
            if (err <= ON_TARGET_DEG or self._ref is None
                    or pointing_error(here[0], here[1], self._ref[0], self._ref[1]) >= PROGRESS_DEG):
                self._ref, self._ref_at = here, now
            elif now - self._ref_at >= STALL_S:
                stopped = self._link.stop()
                self.armed = False
                self._forget_target()
                self._set("stalled",
                          f"{err:.0f} deg off target and not moving for "
                          f"{now - self._ref_at:.0f} s; stopped and disarmed"
                          + ("" if stopped else " (the stop was NOT acknowledged)"))
                return
            self._set(phase, f"{note}; {err:.1f} deg off" if err > ON_TARGET_DEG else f"{note}; on target")

    def _target_for(self, d: dict, sat: str):
        if d.get("error"):
            return None, "armed", f"no live data for {sat}"
        p = d.get("pass")
        if not p:
            return None, "armed", f"no pass for {sat} in the next 24 h"
        if d.get("state") == "live":
            g = d.get("geometry") or {}
            if g.get("az") is None or g.get("el") is None:
                return None, "armed", f"{sat} is up but has no bearing"
            flipped, note = self._plan(p, sat)
            return (self._mount(float(g["az"]), float(g["el"]), flipped), "following",
                    f"following {sat}{note}")
        secs = d.get("seconds_to_aos")
        if secs is not None and secs <= PREPOSITION_S and p.get("aos_az") is not None:
            flipped, note = self._plan(p, sat)
            return (self._mount(float(p["aos_az"]), 0.0, flipped), "pre-positioning",
                    f"at AOS bearing for {sat}, AOS in {secs} s{note}")
        return None, "armed", (f"{sat}: AOS in {secs} s" if secs is not None else f"waiting for {sat}")

    # ---- the mount's limits ---------------------------------------------------
    def limits(self) -> tuple[float, float]:
        """(lowest elevation to point at, the mount's elevation travel: 90 or 180)."""
        raw = self._limits() or {}
        try:
            el_max = 180.0 if float(raw.get("el_max", 90.0)) >= 180.0 else 90.0
        except (TypeError, ValueError):
            el_max = 90.0
        try:
            el_min = min(max(float(raw.get("el_min", 0.0)), 0.0), 45.0)
        except (TypeError, ValueError):
            el_min = 0.0
        return el_min, el_max

    def _plan(self, p: dict, sat: str) -> tuple[bool, str]:
        """Decide ONCE per pass whether to work it flipped, and say what to expect.

        A pass that crosses north takes a 0-360 azimuth rotator through its end
        stop: it has to swing the long way round, and loses the bird while it
        does. A mount with 0-180 elevation can avoid that by pointing 'over the
        top': azimuth + 180, elevation 180 - el, which is the same direction in
        the sky and crosses south instead. The choice is fixed for the whole
        pass; changing it mid-pass would be a half-turn in azimuth."""
        key = f"{sat}|{p.get('aos')}"
        if self._flip is None or self._flip[0] != key:
            north = crosses_north(p.get("track") or [])
            flip = north and self.limits()[1] >= 180.0
            self._flip = (key, flip, north)
            if flip:
                self._logit(f"rotator: {sat} crosses north, working this pass flipped "
                            f"(azimuth + 180, elevation over the top)")
            elif north:
                self._logit(f"rotator: {sat} crosses north and this mount cannot flip; "
                            f"expect a long swing mid-pass", "warn")
        _, flip, north = self._flip
        return flip, (" (flipped)" if flip else " (crosses north: long swing)" if north else "")

    def _mount(self, az: float, el: float, flipped: bool) -> tuple[float, float]:
        """Sky bearing -> what to send the mount, inside its limits."""
        el_min, _ = self.limits()
        el = min(max(el, el_min), 90.0)
        if flipped:
            return (az + 180.0) % 360.0, 180.0 - el
        return az % 360.0, el

    def _release(self) -> None:
        """Nothing to point at any more: stop once, not every second."""
        if self._moving:
            if not self._link.stop():
                self._logit("rotator: the stop at the end of the pass was NOT acknowledged", "bad")
        self._forget_target()

    def _forget_target(self) -> None:
        self.want = None
        self.last_cmd = None
        self._ref = None
        self._moving = False

    def _set(self, state: str, detail: str = "") -> None:
        changed = state != self.state
        self.state, self.detail = state, detail
        if changed and state in ("following", "pre-positioning", "stalled", "unreachable", "refused"):
            cls = {"following": "good", "pre-positioning": "", "stalled": "bad",
                   "unreachable": "bad", "refused": "bad"}[state]
            self._logit(f"rotator {state}: {detail}", cls)

    # ---- status -----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            pos = dict(self.pos)
            out = {"configured": bool(pos.get("configured")),
                   "present": bool(pos.get("reachable")),      # the page's existing key
                   "reachable": bool(pos.get("reachable")),
                   "target": pos.get("target", ""),
                   "armed": self.armed, "state": self.state, "detail": self.detail}
            out["el_min"], out["el_max"] = self.limits()
            if self.want is not None and self._flip is not None:
                out["flipped"] = self._flip[1]
            if pos.get("reachable"):
                out["az"], out["el"], out["read_at"] = pos["az"], pos["el"], pos["read_at"]
            if pos.get("error"):
                out["error"] = pos["error"]
            if self.want is not None:
                out["want_az"], out["want_el"] = round(self.want[0], 1), round(self.want[1], 1)
                if pos.get("reachable"):
                    out["error_deg"] = round(pointing_error(pos["az"], pos["el"], *self.want), 1)
            return out

    # ---- the thread -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="rotor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 -- the loop must not die; say so and carry on
                self._logit(f"rotator error (continuing): {type(e).__name__}: {e}", "bad")
            self._stop_evt.wait(TICK_S)

    def shutdown(self) -> None:
        self._stop_evt.set()
