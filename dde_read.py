#!/usr/bin/env python3
"""dde_read.py — read SatPC32's DDE feed and parse it. Windows only.

The thin, swappable reader Shack-Track's design calls for: everything that
knows about DDE lives here, so if SatPC32 is ever replaced by Gpredict/rotctld
only this file changes.

Format observed live 2026-08-28 (SatPC32 V.12.9) -- see DDE-FORMAT.md:

    SNRS-44 AZ19.3 EL-46.5 UP145966847 UMLSB DN435634488 DMUSB MA120.4 RR3.792985

Prefix immediately precedes its value with NO separator, so parse by prefix.
"""
from __future__ import annotations
import re
import sys

FIELDS = {"SN": ("name", str), "AZ": ("az_deg", float), "EL": ("el_deg", float),
          "UP": ("up_hz", int), "UM": ("up_mode", str), "DN": ("dn_hz", int),
          "DM": ("dn_mode", str), "MA": ("mean_anomaly", float),
          "RR": ("range_rate_kms", float)}

TOKEN = re.compile(r"\b(SN|AZ|EL|UP|UM|DN|DM|MA|RR)(-?[\w.\-+]*)")


def parse(raw: str) -> dict | None:
    """Parse one payload. None when no satellite is selected or in range."""
    if not raw or "NO SATELLITE" in raw.upper():
        return None
    out: dict = {}
    for prefix, value in TOKEN.findall(raw.strip()):
        key, cast = FIELDS[prefix]
        try:
            out[key] = cast(value)
        except ValueError:
            out[key] = value            # keep the raw text rather than drop it
    # Elevation is the one field a caller must never guess at.
    if "el_deg" in out:
        out["above_horizon"] = out["el_deg"] > 0
    return out or None


def read_once(server: str = "SatPC32") -> str:
    import win32ui                       # MUST precede `import dde` (MFC init)
    import dde
    srv = dde.CreateServer()
    srv.Create("shacktrack")
    conv = dde.CreateConversation(srv)
    conv.ConnectTo(server, "SatPcDdeConv")
    return conv.Request("SatPcDdeItem")


if __name__ == "__main__":
    try:
        raw = read_once()
    except Exception as e:               # noqa: BLE001
        print(f"  DDE unavailable: {type(e).__name__}: {e}")
        sys.exit(1)
    print("  raw:", repr(raw))
    d = parse(raw)
    if d is None:
        print("  parsed: no satellite selected / not in range")
    else:
        for k, v in d.items():
            print(f"    {k:16s} {v}")
