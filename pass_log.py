#!/usr/bin/env python3
"""pass_log.py — record a satellite pass: what SatPC32 computes, and what the
radio actually does, sampled together.

RECEIVE ONLY. This script never transmits and never writes to the radio. It
reads the SatPC32 DDE feed and, optionally, the IC-9700's frequency over CI-V.
SatPC32 is what drives the radio; this only watches.

Why both sides: the DDE feed says what SatPC32 *asked* for. Reading the radio
says what it actually *did*. Logging one without the other proves nothing about
the interaction -- the same reason the AX.25 work needed a wire tap and a
source, not either alone.

Output is a CSV plus a live console line, so the pass can be replayed and
plotted afterwards rather than only watched.

    python pass_log.py                        # DDE only, 1 Hz, until Ctrl-C
    python pass_log.py --civ COM21            # also poll the radio's VFO
    python pass_log.py --minutes 12           # stop after N minutes

The CI-V read is a bare 0x03 "read operating frequency" request. It is the one
CI-V command that cannot change anything -- no PTT, no mode, no memory write.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- DDE side ---
FIELDS = {"SN": ("name", str), "AZ": ("az_deg", float), "EL": ("el_deg", float),
          "UP": ("up_hz", int), "UM": ("up_mode", str), "DN": ("dn_hz", int),
          "DM": ("dn_mode", str), "MA": ("mean_anomaly", float),
          "RR": ("range_rate_kms", float)}

import re
TOKEN = re.compile(r"\b(SN|AZ|EL|UP|UM|DN|DM|MA|RR)(-?[\w.\-+]*)")


def parse(raw: str) -> dict | None:
    if not raw or "NO SATELLITE" in raw.upper():
        return None
    out: dict = {}
    for prefix, value in TOKEN.findall(raw.strip()):
        key, cast = FIELDS[prefix]
        try:
            out[key] = cast(value)
        except ValueError:
            out[key] = value
    return out or None


class Dde:
    """Thin DDE reader. Reconnects if SatPC32 restarts mid-pass."""

    def __init__(self, server="SatPC32"):
        self.server, self.conv = server, None

    def _connect(self):
        import win32ui                    # MUST precede `import dde` (MFC init)
        import dde
        srv = dde.CreateServer()
        srv.Create(f"passlog{int(time.time())%10000}")
        conv = dde.CreateConversation(srv)
        conv.ConnectTo(self.server, "SatPcDdeConv")
        self.conv = conv

    def read(self) -> str | None:
        try:
            if self.conv is None:
                self._connect()
            return self.conv.Request("SatPcDdeItem")
        except Exception:
            self.conv = None              # force a reconnect next tick
            return None


# ---------------------------------------------------------------- CI-V side --
def civ_read_freq(port: str, baud: int = 19200, addr: int = 0xA2) -> int | None:
    """Read the IC-9700's operating frequency. RECEIVE-ONLY command 0x03.

    Returns Hz, or None if the port is busy/absent. SatPC32 normally owns this
    port for CAT, so this usually returns None -- that is expected, not an
    error, and the DDE side still logs.
    """
    try:
        import serial
    except ImportError:
        return None
    try:
        with serial.Serial(port, baud, timeout=0.4) as s:
            s.write(bytes([0xFE, 0xFE, addr, 0xE0, 0x03, 0xFD]))
            resp = s.read(32)
    except Exception:
        return None
    # Find the echoed reply: FE FE E0 <addr> 03 <5 BCD bytes LSB-first> FD
    i = resp.find(bytes([0xFE, 0xFE, 0xE0]))
    if i < 0 or len(resp) < i + 11:
        return None
    bcd = resp[i + 5:i + 10]
    hz, mul = 0, 1
    for b in bcd:                          # little-endian packed BCD
        hz += (b & 0x0F) * mul + ((b >> 4) & 0x0F) * mul * 10
        mul *= 100
    return hz


# ------------------------------------------------------------------- main ----
def main() -> int:
    ap = argparse.ArgumentParser(description="Log a satellite pass (receive only).")
    ap.add_argument("--civ", metavar="COM", help="also poll the radio's VFO, e.g. COM21")
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = until Ctrl-C)")
    ap.add_argument("--out", default=None, help="CSV path (default: pass-<sat>-<date>.csv)")
    args = ap.parse_args()

    dde_r = Dde()
    rows, started = [], time.time()
    name_seen = "unknown"
    print("  logging — Ctrl-C to stop. Receive only; nothing is transmitted.")
    print("  time      sat      az     el     down Hz     RR km/s   radio Hz")
    try:
        while True:
            if args.minutes and (time.time() - started) > args.minutes * 60:
                break
            now = dt.datetime.now(dt.timezone.utc)
            d = parse(dde_r.read() or "")
            radio = civ_read_freq(args.civ, args.baud) if args.civ else None
            if d:
                name_seen = d.get("name", name_seen)
                row = {"utc": now.isoformat(timespec="seconds"), **d,
                       "radio_hz": radio if radio else ""}
                rows.append(row)
                print(f"  {now:%H:%M:%S}  {d.get('name',''):<8} "
                      f"{d.get('az_deg',0):5.1f}  {d.get('el_deg',0):5.1f}  "
                      f"{d.get('dn_hz',0):>10}  {d.get('range_rate_kms',0):+8.3f}   "
                      f"{radio if radio else '—':>10}", flush=True)
            else:
                print(f"  {now:%H:%M:%S}  (no satellite in range)", end="\r", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  stopped.")

    if not rows:
        print("  nothing captured — was a satellite above the horizon?")
        return 1
    out = Path(args.out or f"pass-{name_seen}-{dt.date.today()}.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    els = [r["el_deg"] for r in rows if "el_deg" in r]
    dns = [r["dn_hz"] for r in rows if "dn_hz" in r]
    print(f"  wrote {out}  ({len(rows)} samples)")
    if els:
        print(f"  peak elevation {max(els):.1f}°")
    if dns:
        print(f"  downlink swept {min(dns)} .. {max(dns)} Hz "
              f"({(max(dns)-min(dns))/1000:.1f} kHz of Doppler)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
