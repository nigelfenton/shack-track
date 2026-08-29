#!/usr/bin/env python3
"""keps-watch.py — say something when Keplerian data goes stale.

WHY THIS EXISTS
---------------
SatPC32's keps sat at epoch 26022 (late January) for about seven months and
nothing said so. Every pass prediction in that window was minutes off in time
and degrees off in pointing. The cause was that Celestrak moved to
celestrak.org and the old /NORAD/elements/*.txt paths began returning 404, so
the in-app "Update Keps" failed silently.

Silence is the bug. This checks the sources and the local files, and reports.

⭐⭐ COUNT TLE LINES, NEVER HTTP STATUS.
Celestrak answers an unknown GROUP with **HTTP 200** and a body reading
"Invalid query: ... not found". A status-code check calls that success. The
only honest test is whether TLE records actually came back, so every check
here counts lines beginning "1 " and ignores the status entirely.

The same trap sits in ~/bin/render-fd-sats.py on shack-hub, whose refresh_tle()
ends in `except Exception: pass` -- a failed fetch there is swallowed and the
script silently reuses whatever stale file is on disk.

WHAT IT CHECKS
--------------
  sources  each configured URL really returns TLEs
  files    the newest EPOCH inside each local keps file, not its mtime
           (a file re-downloaded today can still contain January elements)

Exit codes:  0 all well · 1 warning · 2 stale or a dead source
so cron mail, a systemd unit or a Nagios-style check can all use it directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Verified working 2026-08-28. The bare celestrak.com/NORAD/elements/*.txt
# paths these replaced now 404; GROUP=noaa and GROUP=tle-new no longer exist
# at all (NOAA birds live inside GROUP=weather).
SOURCES = {
    "amsat-nasabare": "https://www.amsat.org/tle/current/nasabare.txt",
    "celestrak-amateur":
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
    "celestrak-cubesat":
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=cubesat&FORMAT=tle",
}

# Local keps files to age-check. Paths that do not exist are skipped quietly --
# this is meant to run on either the shack-hub or the Windows box.
LOCAL_FILES = [
    Path.home() / "AppData/Roaming/SatPC32/InterKeps2.txt",
    Path.home() / "AppData/Roaming/SatPC32/nasabare.txt",
    Path.home() / "satpass/amateur.tle",
]

WARN_DAYS, STALE_DAYS = 7, 14
MIN_TLES = 10          # a source returning fewer than this is broken, not thin
TIMEOUT = 25
UA = "shack-track-keps-watch/1.0 (+G0JKN)"


def utcnow() -> dt.datetime:
    """Naive UTC. TLE epochs decode naive, so keep both sides naive."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def tle_epochs(text: str) -> list[dt.datetime]:
    """Every epoch in a TLE set, decoded from line 1 columns 19-32.

    Two-digit year: 57-99 means 19xx, 00-56 means 20xx (the NORAD convention).
    """
    out = []
    for line in text.splitlines():
        if not line.startswith("1 ") or len(line) < 32:
            continue
        try:
            yy = int(line[18:20])
            day = float(line[20:32])
        except ValueError:
            continue
        year = 2000 + yy if yy < 57 else 1900 + yy
        try:
            out.append(dt.datetime(year, 1, 1) + dt.timedelta(days=day - 1))
        except (ValueError, OverflowError):
            continue
    return out


def check_source(name: str, url: str) -> dict:
    """Fetch one source and judge it by TLE COUNT, never by status code."""
    result = {"name": name, "url": url, "tles": 0, "state": "dead", "detail": ""}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        result["detail"] = f"HTTP {e.code}"
        return result
    except Exception as e:                      # noqa: BLE001 - report anything
        result["detail"] = f"{type(e).__name__}: {e}"
        return result

    epochs = tle_epochs(body)
    result["tles"] = len(epochs)

    # THE TRAP: 200 OK with an error body. Only the payload is evidence.
    if not epochs:
        first = body.strip().splitlines()[0][:70] if body.strip() else "(empty)"
        result["detail"] = f"200 OK but no TLEs — {first}"
        return result

    if len(epochs) < MIN_TLES:
        result["state"], result["detail"] = "warn", f"only {len(epochs)} TLEs"
        return result

    age = (utcnow() - max(epochs)).total_seconds() / 86400
    result["age_days"] = round(age, 1)
    result["state"] = "ok" if age <= WARN_DAYS else (
        "warn" if age <= STALE_DAYS else "stale")
    result["detail"] = f"{len(epochs)} TLEs, newest epoch {age:.1f} d old"
    return result


def check_file(path: Path) -> dict | None:
    """Age a local keps file by its newest EPOCH, not its mtime.

    The distinction matters: a file downloaded an hour ago can still be full of
    January elements, and mtime would call that fresh.
    """
    if not path.exists():
        return None
    result = {"name": path.name, "path": str(path), "state": "dead", "detail": ""}
    try:
        epochs = tle_epochs(path.read_text("utf-8", "replace"))
    except OSError as e:
        result["detail"] = str(e)
        return result
    if not epochs:
        result["detail"] = "no TLE records found"
        return result
    age = (utcnow() - max(epochs)).total_seconds() / 86400
    result.update(tles=len(epochs), age_days=round(age, 1))
    result["state"] = "ok" if age <= WARN_DAYS else (
        "warn" if age <= STALE_DAYS else "stale")
    result["detail"] = f"{len(epochs)} TLEs, newest epoch {age:.1f} d old"
    return result


SYMBOL = {"ok": "ok  ", "warn": "WARN", "stale": "STALE", "dead": "DEAD"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing when everything is well (for cron)")
    args = ap.parse_args()

    checks = [check_source(n, u) for n, u in SOURCES.items()]
    checks += [c for c in (check_file(p) for p in LOCAL_FILES) if c]

    worst = 0
    for c in checks:
        if c["state"] in ("stale", "dead"):
            worst = 2
        elif c["state"] == "warn" and worst < 1:
            worst = 1

    if args.json:
        print(json.dumps({"checked": utcnow().isoformat() + "Z",
                          "exit": worst, "checks": checks}, indent=2))
        return worst

    if args.quiet and worst == 0:
        return 0

    print(f"keps-watch {utcnow():%Y-%m-%d %H:%M}Z")
    for c in checks:
        print(f"  [{SYMBOL[c['state']]}] {c['name']:<20} {c['detail']}")
    if worst:
        print("\n  Stale keps mean predictions that are minutes off in time and\n"
              "  degrees off in pointing. Refresh before trusting a pass.")
    return worst


if __name__ == "__main__":
    sys.exit(main())
