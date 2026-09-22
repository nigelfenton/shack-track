#!/usr/bin/env python3
"""sat_capture.py -- capture a satellite pass and JUDGE it, not just log it.

RECEIVE ONLY. Sends rigctl 'f' (get frequency) and nothing else. No PTT, no
mode change, no write of any kind reaches the radio.

Why this exists rather than rigctl_log.py: a logger that is working perfectly
and a radio that is not being driven produce identical-looking output. On
2026-08-29 an ISS capture ran 2090 samples at a clean 1 Hz containing no pass
at all -- 1811 of them bit-identical. Sample count and logger health prove
nothing. So this records the PREDICTION (Skyfield) beside the REALITY (the
radio's own VFO) and applies two falsifiable checks at the end:

  1. Magnitude -- Doppler is bounded by the orbit: f_rest * v_max / c.
     Anything past that bound is a wrong VFO or a memory channel, not Doppler.
  2. Shape -- a real pass reverses direction EXACTLY ONCE, at TCA, where the
     range rate crosses zero. Zero reversals = something settling toward a
     target. Two or more = a geometry error.

It also refuses to start quietly if the radio is not reachable, and warns at
AOS if the frequency is not actually moving -- because on both previous
failures the config was correct and the tracker had simply stopped.

    python sat_capture.py --preflight            # check everything, capture nothing
    python sat_capture.py --minutes 25           # capture, then judge
    python sat_capture.py --dry-run              # geometry only, no radio needed
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import socket
import sys
import time

C_KMS = 299792.458

# RS-44 downlink. The transponder is 70 cm down / 2 m up; we watch the downlink
# because that is the side the radio is tuned to on receive.
DEFAULT_DOWNLINK_HZ = 435_645_000

# The station QTH comes from Shack-Track's own satellites.json (SHACKTRACK_SATS,
# as server.py reads it), never from another program's config: Gpredict shipped
# a sample.qth set to Copenhagen and it cost 74 deg of azimuth before anyone
# noticed. The satellites.json in this repo holds a placeholder QTH (Ascension
# Island, II22TB); a station points SHACKTRACK_SATS at a local copy with its own.
def _load_qth():
    import json
    import os
    from pathlib import Path
    here = Path(__file__).resolve().parent
    path = Path(os.environ.get("SHACKTRACK_SATS", here / "satellites.json"))
    q = json.loads(path.read_text(encoding="utf-8"))["qth"]
    return float(q["lat"]), float(q["lon"]), float(q.get("alt_m", 0))


QTH_LAT, QTH_LON, QTH_ALT_M = _load_qth()


def rigctl(host, port, cmd, timeout=3):
    """Send one rigctl command. Returns the reply text, or None if unreachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(cmd + b"\n")
            return s.recv(200).decode("utf-8", "replace").strip()
    except Exception:
        return None


def load_sat(tle_path, name):
    from skyfield.api import EarthSatellite, load as sf_load
    ts = sf_load.timescale()
    lines = open(tle_path, encoding="utf-8", errors="replace").read().splitlines()
    for i, l in enumerate(lines):
        if name.upper() in l.upper():
            return EarthSatellite(lines[i + 1], lines[i + 2], l.strip(), ts), ts
    raise SystemExit("  %s not found in %s" % (name, tle_path))


def geometry(sat, qth, ts_time):
    """Return (az_deg, el_deg, range_rate_km_s) at one instant."""
    topo = (sat - qth).at(ts_time)
    alt, az, _ = topo.altaz()
    rr = topo.frame_latlon_and_rates(qth)[5].km_per_s
    return az.degrees, alt.degrees, rr


def verdict(rows, downlink_hz, dry_run):
    """Apply the two checks. Returns (ok, list_of_lines)."""
    out = []
    ok = True

    # --- Check 1: magnitude, bounded by the orbit -------------------------
    bound = downlink_hz * 7.8 / C_KMS  # 7.8 km/s is a hard LEO ceiling
    have_radio = [r for r in rows if r["radio_hz"]]

    if dry_run:
        out.append("  radio    : not captured (dry run)")
    elif not have_radio:
        out.append("  ** NO RADIO SAMPLES -- nothing to judge **")
        ok = False
    else:
        hz = [int(r["radio_hz"]) for r in have_radio]
        offs = [h - downlink_hz for h in hz]
        peak = max(abs(o) for o in offs)
        distinct = len(set(hz))
        out.append("  samples  : %d  (%d distinct values)" % (len(hz), distinct))
        out.append("  swept    : %d .. %d Hz  (%.2f kHz)"
                   % (min(hz), max(hz), (max(hz) - min(hz)) / 1000))
        out.append("  peak off : %+.0f Hz   (orbit bound %.0f Hz)" % (peak, bound))

        if distinct < len(hz) * 0.2:
            out.append("  ** FLAT: most samples identical -- the radio was NOT being driven **")
            ok = False
        if peak > bound:
            out.append("  ** OUT OF BOUNDS: %.0f Hz exceeds %.0f -- wrong VFO or memory channel **"
                       % (peak, bound))
            ok = False
        else:
            out.append("  magnitude: OK -- within the orbital bound")

    # --- Check 2: shape, exactly one reversal ------------------------------
    # Judge on the PREDICTED range rate, which is present even in a dry run.
    rr = [float(r["range_rate_kms"]) for r in rows if r["range_rate_kms"] != ""]
    if len(rr) > 2:
        sign = [1 if x > 0 else -1 for x in rr]
        rev = sum(1 for a, b in zip(sign, sign[1:]) if a != b)
        out.append("  reversals: %d (predicted range rate; a real pass = exactly 1)" % rev)
        if rev == 1:
            out.append("  shape    : OK -- single TCA crossing captured")
        elif rev == 0:
            out.append("  ** NO TCA: capture did not span closest approach -- one side only **")
            ok = False
        else:
            out.append("  ** %d REVERSALS: geometry error, not a clean pass **" % rev)
            ok = False

    el = [float(r["el_deg"]) for r in rows if r["el_deg"] != ""]
    if el:
        out.append("  max elev : %.1f deg" % max(el))
        if max(el) <= 0:
            out.append("  ** BELOW HORIZON THROUGHOUT -- this is not a pass **")
            ok = False

    return ok, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sat", default="RS-44")
    ap.add_argument("--tle", default="amateur.tle")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4532)
    ap.add_argument("--downlink", type=int, default=DEFAULT_DOWNLINK_HZ)
    ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="log geometry only; never contact the radio")
    ap.add_argument("--preflight", action="store_true",
                    help="check TLE, QTH, radio and pass geometry, then exit")
    a = ap.parse_args()

    from skyfield.api import wgs84
    sat, ts = load_sat(a.tle, a.sat)
    qth = wgs84.latlon(QTH_LAT, QTH_LON, elevation_m=QTH_ALT_M)

    now = ts.now()
    age = now.tt - sat.epoch.tt
    print("  satellite : %s" % sat.name)
    print("  TLE epoch : %s  (%.2f d old)"
          % (sat.epoch.utc_strftime("%Y-%m-%d %H:%M UTC"), age))
    if age > 7:
        print("  ** TLE IS STALE (>7 d) -- refresh before trusting this **")
    print("  QTH       : %s, %s  alt %s m" % (QTH_LAT, QTH_LON, QTH_ALT_M))
    print("  downlink  : %.3f MHz" % (a.downlink / 1e6))

    az, el, rr = geometry(sat, qth, now)
    print("  right now : az %.1f  el %.1f  range rate %+.3f km/s" % (az, el, rr))

    # Radio reachability -- fail loudly here rather than capture 2000 rows of nothing
    if not a.dry_run:
        fq = rigctl(a.host, a.port, b"f")
        if fq and fq.lstrip("-").isdigit():
            print("  radio     : OK -- %s:%d reports %.6f MHz"
                  % (a.host, a.port, int(fq) / 1e6))
        else:
            print("  ** RADIO NOT REACHABLE on %s:%d (rigctl 'f' gave %r) **"
                  % (a.host, a.port, fq))
            print("     Start AetherSDR and confirm its rigctl server is listening.")
            if not a.preflight:
                return 1

    if a.preflight:
        t1 = ts.tt_jd(now.tt + 1.0)
        times, events = sat.find_events(qth, now, t1, altitude_degrees=0.0)
        print("\n  next passes (next 24 h, local):")
        cur = {}
        for t, e in zip(times, events):
            if e == 0:
                cur = {"rise": t}
            elif e == 1 and cur:
                cur["peak"] = t
            elif e == 2 and cur.get("peak") is not None:
                pk_el = geometry(sat, qth, cur["peak"])[1]
                aos = cur["rise"].utc_datetime().astimezone()
                dur = (t.tt - cur["rise"].tt) * 86400
                print("    %s  %dm%02ds  max el %5.1f"
                      % (aos.strftime("%a %d %b %H:%M:%S"),
                         int(dur // 60), int(dur % 60), pk_el))
                cur = {}
        print("\n  preflight only -- nothing captured.")
        return 0

    print("\n  capturing for %.0f min. RECEIVE ONLY. Ctrl-C to stop early.\n" % a.minutes)
    rows, t0, last, moved = [], time.time(), None, False
    try:
        while time.time() - t0 < a.minutes * 60:
            t = ts.now()
            az, el, rr = geometry(sat, qth, t)
            hz = None if a.dry_run else rigctl(a.host, a.port, b"f")
            hz = hz if (hz and hz.lstrip("-").isdigit()) else None
            pred = -a.downlink * rr / C_KMS

            rows.append({
                "utc": t.utc_datetime().isoformat(timespec="seconds"),
                "az_deg": "%.1f" % az,
                "el_deg": "%.1f" % el,
                "range_rate_kms": "%.4f" % rr,
                "predicted_doppler_hz": "%.0f" % pred,
                "radio_hz": hz or "",
            })

            if hz and hz != last:
                moved = True
                off = int(hz) - a.downlink
                print("  %s  el %5.1f  radio %.6f  off %+6d Hz  pred %+6.0f"
                      % (t.utc_datetime().astimezone().strftime("%H:%M:%S"),
                         el, int(hz) / 1e6, off, pred), flush=True)
                last = hz
            # Engagement check: 90 s above the horizon with a frozen VFO is the
            # exact signature of a tracker that is not actually running.
            if el > 0 and not moved and time.time() - t0 > 90:
                print("  ** WARNING: above horizon but the frequency has not moved. **")
                print("     Check the tracker is running and engaged NOW -- not the config.")
                moved = True  # warn once
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  stopped early.")

    if not rows:
        print("  nothing captured")
        return 1

    out = a.out or "%s-capture-%s.csv" % (a.sat.lower(), dt.date.today())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("\n  wrote %s (%d samples)" % (out, len(rows)))
    print("  ---- verdict ----")
    ok, lines = verdict(rows, a.downlink, a.dry_run)
    for l in lines:
        print(l)
    print("  ---- %s ----"
          % ("PASS: this looks like a real tracked pass" if ok
             else "NOT PROVEN: see the flags above"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
