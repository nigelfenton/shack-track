#!/usr/bin/env python3
r"""rotor_sim.py -- a network rotator controller with no rotator: K3NG's wire, simulated motion.

Stands in for a K3NG rotator controller reached over Ethernet or WiFi, so the
rotator side of Shack-Track can be written and tested before any hardware
exists. It speaks the Yaesu GS-232 emulation exactly as K3NG's firmware does
on its Ethernet port (read from k3ng_rotator_controller.ino, process_yaesu_command
and service_ethernet):

  * a command ends with CR; every command gets one reply line ending CR LF,
    which is EMPTY for commands that return nothing (M, W, S, A, E, L, R, U, D, X)
  * C   -> AZ=aaa            C2 -> AZ=aaaEL=eee   (no space: K3NG's form, not Yaesu's)
    B   -> EL=eee            with --gs232a: +0aaa, +0aaa+0eee, +0eee
  * Maaa        rotate to azimuth          Waaa eee   rotate to azimuth and elevation
  * S all stop, A azimuth stop, E elevation stop
  * L / R manual CCW / CW, U / D manual up / down (until a stop or a target)
  * X1..X4 azimuth speed
  * anything else, or a malformed number -> ?>

The motion is a model, not a recording: constant speed toward the target,
stop inside a tolerance, hard travel limits, and position reported as whole
degrees like the firmware. Defaults are a Yaesu G-5500 (about 58 s per 360 deg
of azimuth, 67 s per 180 deg of elevation). Faults can be injected to test a
client's honesty: --stall az|el|both makes the rotator accept commands and
not move, as with a tripped breaker or a seized bearing.

It also answers Hamlib's rotator network protocol (what `rotctld` speaks, by
convention on TCP 4533) on a second port, driving the SAME simulated rotator,
so a client can be written against either wire and the two can be compared:

  * commands end with LF (a CR before it is tolerated); positions are floats
  * p            -> azimuth line, elevation line        (long form: \get_pos)
  * P az el      -> RPRT 0, or RPRT -1 if out of range   (\set_pos)
  * S stop, K park (--park-az / --park-el)               (\stop, \park)
  * M dir speed  -> manual move: 2 up, 4 down, 8 left (CCW), 16 right (CW)  (\move)
  * _            -> one line of information               (\get_info)
  * q            -> closes the connection
  * a leading + gives the extended reply (name: value lines, then RPRT 0);
    a leading ; | or , gives the same on one line with that separator
  * anything else -> RPRT -1

The rotctld side follows Hamlib's rotctld(1) manual page. Two details are
this simulator's choices, not checked against a running rotctld: positions
are printed with six decimals, and an unknown command answers RPRT -1.

NOT simulated: K3NG's backslash command line, EasyComm, the 450-degree overlap
logic, slow start/stop ramps, and the display. Backslash commands on the
GS-232 port answer ?> here; the firmware answers them properly.

    python rotor_sim.py                         # GS-232 on 127.0.0.1:4023, rotctld on :4533
    python rotor_sim.py --bind 0.0.0.0 --az 180 --el 0 --verbose
    python rotor_sim.py --rotctld-port 0        # GS-232 only

Binds 127.0.0.1 unless told otherwise. It moves nothing real.
"""

import argparse
import socketserver
import threading
import time

AZ_SPEEDS = {1: 0.25, 2: 0.5, 3: 0.75, 4: 1.0}     # X1..X4 as a fraction of full azimuth speed


class RotorModel:
    """Azimuth and elevation that move toward a target at a fixed rate.

    Time is passed in, so a test can step it without sleeping.
    """

    def __init__(self, az=0.0, el=0.0, az_rate=6.2, el_rate=2.7, az_max=360.0, el_max=180.0,
                 az_tol=1.0, el_tol=1.0, stall="", park=(0.0, 0.0)):
        self.park = park
        self.az, self.el = float(az), float(el)
        self.az_rate, self.el_rate = az_rate, el_rate
        self.az_max, self.el_max = az_max, el_max
        self.az_tol, self.el_tol = az_tol, el_tol
        self.stall = stall                      # "", "az", "el" or "both"
        self.az_speed = 1.0
        self.az_target = None                   # degrees, or +1/-1 marker via az_manual
        self.el_target = None
        self.az_manual = 0                      # -1 CCW, +1 CW, 0 none
        self.el_manual = 0
        self._t = None
        self.lock = threading.Lock()

    # -- motion ---------------------------------------------------------------
    def advance(self, now):
        """Move both axes forward to time `now` (seconds, any monotonic origin)."""
        if self._t is None:
            self._t = now
            return
        dt = max(0.0, now - self._t)
        self._t = now
        if self.stall not in ("az", "both"):
            self.az, self.az_target, self.az_manual = self._step(
                self.az, self.az_target, self.az_manual, self.az_rate * self.az_speed * dt,
                0.0, self.az_max, self.az_tol)
        if self.stall not in ("el", "both"):
            self.el, self.el_target, self.el_manual = self._step(
                self.el, self.el_target, self.el_manual, self.el_rate * dt,
                0.0, self.el_max, self.el_tol)

    @staticmethod
    def _step(pos, target, manual, travel, lo, hi, tol):
        if manual:
            pos = min(hi, max(lo, pos + manual * travel))
            if pos in (lo, hi):
                manual = 0                      # ran into the end stop
            return pos, None, manual
        if target is None:
            return pos, None, 0
        gap = target - pos
        if abs(gap) <= tol:
            return pos, None, 0                 # inside tolerance: stop, as the firmware does
        if abs(gap) <= travel:
            return target, None, 0
        return pos + (travel if gap > 0 else -travel), target, 0

    def moving(self):
        return bool(self.az_target is not None or self.el_target is not None
                    or self.az_manual or self.el_manual)

    # -- the wire -------------------------------------------------------------
    def command(self, line, now, gs232a=False):
        """Apply one command (no terminator) and return the reply text, without CR LF."""
        with self.lock:
            self.advance(now)
            line = line.strip()
            if not line:
                return None                     # a bare CR: the firmware ignores an empty buffer
            c, rest = line[0].upper(), line[1:]
            az_i, el_i = int(self.az) % 360 if self.az_max > 360 else int(self.az), int(self.el)
            if gs232a:
                az_s, el_s = "+0%03d" % az_i, "+0%03d" % el_i
            else:
                az_s, el_s = "AZ=%03d" % az_i, "EL=%03d" % el_i
            if c == "C":
                return az_s + el_s if rest.startswith("2") else az_s
            if c == "B":
                return el_s
            if c == "S":
                self.az_target = self.el_target = None
                self.az_manual = self.el_manual = 0
                return ""
            if c == "A":
                self.az_target, self.az_manual = None, 0
                return ""
            if c == "E":
                self.el_target, self.el_manual = None, 0
                return ""
            if c in "LR" and not rest:
                self.az_target, self.az_manual = None, (1 if c == "R" else -1)
                return ""
            if c in "UD" and not rest:
                self.el_target, self.el_manual = None, (1 if c == "U" else -1)
                return ""
            if c == "X":
                if rest in ("1", "2", "3", "4"):
                    self.az_speed = AZ_SPEEDS[int(rest)]
                    return ""
                return "?>"
            if c == "M":
                if len(rest) == 3 and rest.isdigit() and int(rest) <= self.az_max:
                    self.az_target, self.az_manual = float(rest), 0
                    return ""
                return "?>"
            if c == "W":
                parts = rest.split()
                if (len(parts) == 2 and all(len(p) == 3 and p.isdigit() for p in parts)
                        and int(parts[0]) <= self.az_max and int(parts[1]) <= self.el_max):
                    self.az_target, self.az_manual = float(parts[0]), 0
                    self.el_target, self.el_manual = float(parts[1]), 0
                    return ""
                return "?>"
            return "?>"


    # -- Hamlib's rotator network protocol (rotctld) -----------------------------
    ROTCTL_LONG = {"get_pos": "p", "set_pos": "P", "stop": "S", "park": "K", "move": "M",
                   "get_info": "_", "quit": "q"}

    def rotctl(self, line, now):
        """Apply one rotctld command line. Returns the reply text (with its newlines),
        "" for nothing, or None to close the connection."""
        with self.lock:
            self.advance(now)
            line = line.strip()
            if not line:
                return ""
            ext = None
            if line[0] in "+;|,":
                ext = "\n" if line[0] == "+" else line[0]
                line = line[1:].lstrip()
                if not line:
                    return "RPRT -1\n"
            parts = line.split()
            head, args = parts[0], parts[1:]
            if head.startswith("\\"):
                name = head[1:]
                c = self.ROTCTL_LONG.get(name)
            else:
                c = head if head in self.ROTCTL_LONG.values() else None
                name = next((k for k, v in self.ROTCTL_LONG.items() if v == c), "")

            def done(code, fields=()):
                if ext is None:
                    body = "".join("%s\n" % v for _, v in fields)
                    return body + ("" if (fields and code == 0) else "RPRT %d\n" % code)
                out = ["%s:%s" % (name, (" " + " ".join(args)) if args else "")]
                out += ["%s: %s" % kv for kv in fields]
                out.append("RPRT %d" % code)
                return ext.join(out) + "\n"

            if c is None:
                return "RPRT -1\n"
            if c == "q":
                return None
            if c == "p":
                return done(0, (("Azimuth", "%.6f" % self.az), ("Elevation", "%.6f" % self.el)))
            if c == "_":
                return done(0, (("Info", "rotor_sim, simulated rotator, no hardware"),))
            if c == "S":
                self.az_target = self.el_target = None
                self.az_manual = self.el_manual = 0
                return done(0)
            if c == "K":
                self.az_target, self.el_target = self.park
                self.az_manual = self.el_manual = 0
                return done(0)
            if c == "P":
                try:
                    az, el = float(args[0]), float(args[1])
                except (IndexError, ValueError):
                    return done(-1)
                if len(args) != 2 or not (0.0 <= az <= self.az_max and 0.0 <= el <= self.el_max):
                    return done(-1)
                self.az_target, self.az_manual = az, 0
                self.el_target, self.el_manual = el, 0
                return done(0)
            if c == "M":
                try:
                    direction = int(args[0])
                    int(args[1])
                except (IndexError, ValueError):
                    return done(-1)
                if direction == 2:
                    self.el_target, self.el_manual = None, 1
                elif direction == 4:
                    self.el_target, self.el_manual = None, -1
                elif direction == 8:
                    self.az_target, self.az_manual = None, -1
                elif direction == 16:
                    self.az_target, self.az_manual = None, 1
                else:
                    return done(-1)
                return done(0)
            return "RPRT -1\n"


class RotctlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        while True:
            try:
                raw = self.rfile.readline(512)
            except OSError:
                return
            if not raw:
                return
            text = raw.decode("ascii", "replace").replace("\r", "").rstrip("\n")
            reply = srv.model.rotctl(text, time.monotonic())
            if srv.verbose:
                print("%s  rotctl %-14r -> %-34r az %.1f el %.1f" % (
                    time.strftime("%H:%M:%S"), text, reply, srv.model.az, srv.model.el), flush=True)
            if reply is None:
                return
            if reply:
                try:
                    self.request.sendall(reply.encode("ascii"))
                except OSError:
                    return


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        buf = b""
        while True:
            try:
                chunk = self.request.recv(256)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                raw, buf = buf.split(b"\r", 1)
                text = raw.replace(b"\n", b"").decode("ascii", "replace")
                reply = srv.model.command(text, time.monotonic(), srv.gs232a)
                if reply is None:
                    continue
                if srv.verbose:
                    print("%s  %-10r -> %-14r az %.1f el %.1f" % (
                        time.strftime("%H:%M:%S"), text, reply, srv.model.az, srv.model.el), flush=True)
                try:
                    self.request.sendall(reply.encode("ascii") + b"\r\n")
                except OSError:
                    return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, model, gs232a=False, verbose=False, handler=None):
        super().__init__(addr, handler or Handler)
        self.model, self.gs232a, self.verbose = model, gs232a, verbose


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="K3NG-style network rotator controller with simulated motion")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4023, help="GS-232 port; K3NG's own default is 23")
    ap.add_argument("--rotctld-port", type=int, default=4533,
                    help="Hamlib rotator protocol port; 0 turns it off")
    ap.add_argument("--park-az", type=float, default=0.0)
    ap.add_argument("--park-el", type=float, default=0.0)
    ap.add_argument("--az", type=float, default=0.0, help="starting azimuth")
    ap.add_argument("--el", type=float, default=0.0, help="starting elevation")
    ap.add_argument("--az-rate", type=float, default=6.2, help="degrees per second at full speed")
    ap.add_argument("--el-rate", type=float, default=2.7, help="degrees per second")
    ap.add_argument("--az-max", type=float, default=360.0)
    ap.add_argument("--el-max", type=float, default=180.0)
    ap.add_argument("--stall", choices=["az", "el", "both"], default="", help="accept commands, do not move")
    ap.add_argument("--gs232a", action="store_true", help="answer in GS-232A form (+0aaa+0eee)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    m = RotorModel(a.az, a.el, a.az_rate, a.el_rate, a.az_max, a.el_max, stall=a.stall,
                   park=(a.park_az, a.park_el))
    r = None
    if a.rotctld_port:
        r = Server((a.bind, a.rotctld_port), m, verbose=a.verbose, handler=RotctlHandler)
        threading.Thread(target=r.serve_forever, daemon=True).start()
    with Server((a.bind, a.port), m, a.gs232a, a.verbose) as s:
        print("rotor_sim  az %.0f el %.0f%s" % (a.az, a.el, ("  STALLED: " + a.stall) if a.stall else ""))
        print("  %s on %s:%d" % ("GS-232A" if a.gs232a else "GS-232B (K3NG form)", a.bind, a.port))
        print("  rotctld protocol %s" % (("on %s:%d" % (a.bind, a.rotctld_port)) if r else "off"), flush=True)
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if r:
                r.shutdown()
