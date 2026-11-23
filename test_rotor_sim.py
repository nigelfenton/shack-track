#!/usr/bin/env python3
"""test_rotor_sim.py -- the simulated rotator controller answers like K3NG and moves like a rotator.

The simulator is what the rotator side of Shack-Track will be written against,
so what it says on the wire has to be K3NG's, and its motion has to be the
kind a client can be wrong about: it takes time, it stops short inside a
tolerance, it stops at the end of travel, and it can be stalled.

    python test_rotor_sim.py
"""

import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rotor_sim as R

fails = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ---- the model, with time stepped by hand -----------------------------------
m = R.RotorModel(az=10, el=5)
t = 100.0
check("C answers AZ=aaa", m.command("C", t) == "AZ=010", m.command("C", t))
check("C2 answers AZ=aaaEL=eee with no space (K3NG's form)", m.command("C2", t) == "AZ=010EL=005",
      m.command("C2", t))
check("B answers EL=eee", m.command("B", t) == "EL=005", m.command("B", t))
check("GS-232A form is +0aaa+0eee", m.command("C2", t, gs232a=True) == "+0010+0005",
      m.command("C2", t, gs232a=True))
check("M returns an empty reply, not an error", m.command("M100", t) == "", repr(m.command("M100", t)))
check("it has not moved at the instant of the command", m.command("C", t) == "AZ=010", m.command("C", t))
t += 5.0
r = m.command("C", t)
check("after 5 s at 6.2 deg/s it is part-way (about 41), not there", r == "AZ=041", r)
check("and it reports that it is still moving", m.moving())
t += 60.0
r = m.command("C", t)
check("it arrives on the target and stops", r == "AZ=100" and not m.moving(), r)

check("W takes azimuth and elevation together", m.command("W200 045", t) == "")
t += 10.0
r = m.command("C2", t)
check("elevation is slower than azimuth (62 deg of az, 27 of el in 10 s)", r == "AZ=162EL=032", r)
check("E stops elevation only", m.command("E", t) == "")
t += 20.0
r = m.command("C2", t)
check("azimuth carried on to 200, elevation stayed where it was stopped", r == "AZ=200EL=032", r)

check("S stops everything", m.command("M010", t) == "" and m.command("S", t) == "" and not m.moving())
t += 30.0
check("and nothing moves after an all-stop", m.command("C", t) == "AZ=200", m.command("C", t))

check("a target inside the tolerance does not start a move", m.command("M201", t) == "")
t += 5.0
check("so it stays put, as the firmware does", m.command("C", t) == "AZ=200" and not m.moving(),
      m.command("C", t))

check("R runs clockwise until told otherwise", m.command("R", t) == "")
t += 600.0
r = m.command("C", t)
check("and stops at the end of travel, not past it", r == "AZ=360" and not m.moving(), r)
check("L runs counter-clockwise", m.command("L", t) == "")
t += 10.0
check("by 62 degrees in 10 s", m.command("C", t) == "AZ=298", m.command("C", t))
m.command("S", t)

check("X1 slows azimuth to a quarter", m.command("X1", t) == "" and m.command("M200", t) == "")
t += 10.0
check("15.5 degrees in 10 s at X1", m.command("C", t) == "AZ=282", m.command("C", t))
m.command("X4", t); m.command("S", t)

for bad in ("M", "M1", "M999", "W200", "W200 999", "W20 045", "X9", "Q", "\\A"):
    check(f"malformed or unknown {bad!r} answers ?>", m.command(bad, t) == "?>", repr(m.command(bad, t)))
check("a bare CR gets no reply at all", m.command("", t) is None)
check("lower case is accepted", m.command("c", t).startswith("AZ="))

s = R.RotorModel(az=50, el=10, stall="az")
t = 0.0
s.command("W150 060", t)
t += 30.0
r = s.command("C2", t)
check("a stalled azimuth accepts the command and does not move; elevation does", r == "AZ=050EL=060", r)
check("and the model still says it is trying", s.moving())

# ---- over a real socket, one connection, CR in and CR LF out ------------------
srv = R.Server(("127.0.0.1", 0), R.RotorModel(az=123, el=45))
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
with socket.create_connection(("127.0.0.1", port), timeout=2) as c:
    f = c.makefile("rb")
    c.sendall(b"C2\r")
    line = f.readline()
    check("over TCP: C2 answers one CR LF line", line == b"AZ=123EL=045\r\n", repr(line))
    c.sendall(b"M124\r")
    check("over TCP: M answers an empty line", f.readline() == b"\r\n")
    c.sendall(b"C\r\nB\r")
    a, b = f.readline(), f.readline()
    check("over TCP: a stray LF after CR is ignored, and commands can be sent back to back",
          a == b"AZ=123\r\n" and b == b"EL=045\r\n", f"{a!r} {b!r}")
    c.sendall(b"ZZ\r")
    check("over TCP: unknown answers ?>", f.readline() == b"?>\r\n")
with socket.create_connection(("127.0.0.1", port), timeout=2) as c2:
    c2.sendall(b"C\r")
    check("a second client sees the same rotator", c2.makefile("rb").readline() == b"AZ=123\r\n")
srv.shutdown()

# ---- Hamlib's rotator protocol (rotctld), on the same model ------------------
h = R.RotorModel(az=10, el=5, park=(180.0, 0.0))
t = 0.0
check("rotctl p answers azimuth then elevation as floats", h.rotctl("p", t) == "10.000000\n5.000000\n",
      repr(h.rotctl("p", t)))
check("rotctl P answers RPRT 0", h.rotctl("P 72.5 30", t) == "RPRT 0\n", repr(h.rotctl("P 72.5 30", t)))
t += 4.0
r = h.rotctl("p", t)
check("it moves at the same rates as the GS-232 side (24.8 of az, 10.8 of el in 4 s)",
      r == "34.800000\n15.800000\n", repr(r))
check("and the GS-232 side sees the same rotator, in whole degrees", h.command("C2", t) == "AZ=034EL=015",
      h.command("C2", t))
t += 60.0
check("it lands on a fractional target exactly", h.rotctl("p", t) == "72.500000\n30.000000\n",
      repr(h.rotctl("p", t)))
check("a target set by GS-232 is seen by rotctl", h.command("M100", t) == "" and h.moving())
t += 60.0
check("after it arrives", h.rotctl("p", t).startswith("100.000000\n"), repr(h.rotctl("p", t)))
check("S stops", h.rotctl("P 200 60", t) == "RPRT 0\n" and h.rotctl("S", t) == "RPRT 0\n" and not h.moving())
check("K parks at the configured position", h.rotctl("K", t) == "RPRT 0\n" and h.moving())
t += 120.0
check("and gets there", h.rotctl("p", t) == "180.000000\n0.000000\n", repr(h.rotctl("p", t)))
check("M 16 moves right (clockwise) until stopped", h.rotctl("M 16 50", t) == "RPRT 0\n")
t += 5.0
check("31 degrees in 5 s", h.rotctl("p", t).startswith("211.000000\n"), repr(h.rotctl("p", t)))
h.rotctl("S", t)
check("long names work: \\get_pos", h.rotctl("\\get_pos", t).startswith("211.000000\n"))
check("long names work: \\set_pos", h.rotctl("\\set_pos 10 10", t) == "RPRT 0\n")
h.rotctl("\\stop", t)
check("extended reply names the command and the values",
      h.rotctl("+p", t) == "get_pos:\nAzimuth: 211.000000\nElevation: 0.000000\nRPRT 0\n", repr(h.rotctl("+p", t)))
check("extended set echoes its arguments", h.rotctl("+P 90 45", t) == "set_pos: 90 45\nRPRT 0\n",
      repr(h.rotctl("+P 90 45", t)))
h.rotctl("S", t)
check("a separator character puts the extended reply on one line",
      h.rotctl(";p", t) == "get_pos:;Azimuth: 211.000000;Elevation: 0.000000;RPRT 0\n", repr(h.rotctl(";p", t)))
check("_ gives one line of information", h.rotctl("_", t).count("\n") == 1 and "simulated" in h.rotctl("_", t))
for bad in ("P", "P 10", "P 999 0", "P 10 200", "P ten 0", "M 3 50", "M", "z", "\\nonsense", "+"):
    check(f"rotctl {bad!r} answers RPRT -1 and moves nothing",
          h.rotctl(bad, t).endswith("RPRT -1\n") and not h.moving(), repr(h.rotctl(bad, t)))
check("an empty line gets no reply", h.rotctl("", t) == "")
check("q asks for the connection to be closed", h.rotctl("q", t) is None)

both = R.RotorModel(az=40, el=20)
g = R.Server(("127.0.0.1", 0), both)
k = R.Server(("127.0.0.1", 0), both, handler=R.RotctlHandler)
for s_ in (g, k):
    threading.Thread(target=s_.serve_forever, daemon=True).start()
with socket.create_connection(("127.0.0.1", k.server_address[1]), timeout=2) as c:
    f = c.makefile("rb")
    c.sendall(b"p\n")
    check("over TCP: rotctl p answers two lines", (f.readline(), f.readline()) == (b"40.000000\n", b"20.000000\n"))
    c.sendall(b"P 41 20\r\n")
    check("over TCP: a CR before the LF is tolerated", f.readline() == b"RPRT 0\n")
    with socket.create_connection(("127.0.0.1", g.server_address[1]), timeout=2) as c2:
        c2.sendall(b"C2\r")
        check("the GS-232 port serves the same rotator at the same time",
              c2.makefile("rb").readline() == b"AZ=040EL=020\r\n")
    c.sendall(b"q\n")
    check("over TCP: q closes the connection", f.readline() == b"")
g.shutdown(); k.shutdown()

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
