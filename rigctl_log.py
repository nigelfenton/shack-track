#!/usr/bin/env python3
"""rigctl_log.py — record the radio's frequency while something else drives it.

RECEIVE ONLY: sends 'f' (get frequency) and 'm' (get mode) to a rigctl server.
Neither can change anything. This watches Gpredict drive AetherSDR drive the
IC-9700 -- the half that a Doppler-source log cannot prove on its own.
"""
import argparse, csv, datetime as dt, socket, sys, time

def ask(host, port, cmd, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(cmd + b"\n")
            return s.recv(200).decode("utf-8", "replace").strip()
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4532)
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rows, t0, last = [], time.time(), None
    print("  watching the radio (receive only). Ctrl-C to stop.")
    try:
        while time.time() - t0 < a.minutes * 60:
            hz = ask(a.host, a.port, b"f")
            now = dt.datetime.now(dt.timezone.utc)
            if hz and hz.lstrip("-").isdigit():
                rows.append({"utc": now.isoformat(timespec="seconds"), "hz": int(hz)})
                if hz != last:
                    d = "" if last is None else f"  ({int(hz)-int(last):+d} Hz)"
                    print(f"  {now:%H:%M:%S}  {int(hz)/1e6:.6f} MHz{d}", flush=True)
                    last = hz
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  stopped.")

    if not rows:
        print("  nothing captured"); return 1
    out = a.out or f"radio-{dt.date.today()}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["utc", "hz"]); w.writeheader(); w.writerows(rows)
    hz = [r["hz"] for r in rows]
    print(f"  wrote {out} ({len(rows)} samples)")
    print(f"  swept {min(hz)} .. {max(hz)} Hz  ({(max(hz)-min(hz))/1000:.2f} kHz)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
