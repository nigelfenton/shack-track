# Shack-Track — weekend one

A concrete plan for a first working version. Scope is deliberately small: the
thing that earns its place is a **pass list and a live view in a browser**, on
every machine in the shack, with no per-platform build.

## Why this is smaller than it sounds

Four things that would normally be the hard parts are already done:

| | |
|---|---|
| **The orbital maths** | Skyfield, already installed on shack-hub and already computing FD passes in `render-fd-sats.py`. Validated tonight: AOS within **14 s** of SatPC32's independent SGP4. |
| **The CAT path** | AetherSDR **is** the rigctl server on port 4532. No Hamlib. Verified returning `145210000` / `FM`. |
| **Keps health** | `keps-watch.py` — running daily, counts TLE lines rather than trusting HTTP status. |
| **The interface** | Three concepts already drawn against real DDE field names in `design/interface-concepts.html`. |

⭐ **Proven 2026-08-29:** shack-hub reached `10.0.0.104:4532` across the LAN and
got the radio's real frequency back. So the server can live on the hub and still
drive the radio on aurora13 — no code on the Windows box at all.

## Architecture

    shack-hub (Debian, always on)
      +-- Skyfield          pass prediction, az/el, Doppler
      +-- keps-watch.py     already running, already correct
      +-- HTTP + WebSocket  serves the UI, pushes 1 Hz updates
              |
              +--> rigctl  10.0.0.104:4532   AetherSDR -> IC-9700
              +--> GS-232  (later)            K3NG rotator

    any browser: aurora13, laptop, Pi, Mac mini, phone

One codebase. No GTK, no Qt, no MSYS2, no per-platform builds, and nothing to
maintain as "the Windows build of someone else's project".

## Weekend one — three things only

**1. Pass engine** (`passes.py`)
Wrap what `render-fd-sats.py` already does: given a QTH and a satellite list,
return the next N passes as JSON — AOS, LOS, peak elevation, duration, and the
az/el track. That file is the reference implementation; this is a refactor, not
new work.

**2. Server** (`server.py`)
Flask + a WebSocket. Three endpoints:
- `GET /api/passes` — the next 12 hours
- `GET /api/keps` — `keps-watch.py --json`, so staleness is visible
- `WS /live` — 1 Hz: current satellite az/el, Doppler-corrected up/down, and
  the radio's actual frequency read back from rigctl

⚠ `pip install flask` is the only new dependency. Skyfield is already there.

**3. UI** — Concept B then Concept C
Start with the **pass list** (Concept B): bar length is time above horizon,
colour is elevation class. It answers "is anything worth staying up for", needs
no live data, and is useful the moment it renders.

Then the **status strip** (Concept C), which carries keps age — the thing you
cannot see from the radio and the failure this project started from.

**Concept A (the operating view) is weekend two.** It needs the polar plot, live
Doppler and rotator bearing, and it is only useful during a pass.

## Explicitly NOT in weekend one

- **No SGP4 of our own.** Borrowed, permanently.
- **No rotator control.** Read-only until the hardware is present and the path
  is proven; it is the only piece that MOVES anything.
- **No transmit.** Ever, from this tool.
- **No DDE / SatPC32 dependency.** Skyfield replaces it outright. `dde_read.py`
  stays as a way to cross-check against SatPC32, not as a runtime dependency.

## The one real risk

**Doppler correction has to be written and verified**, where SatPC32 and
Gpredict give it for free. The physics is simple —
`f_observed = f_rest * (1 - range_rate/c)` — and Skyfield gives range rate
directly, but "simple" is how tonight's polar-plot errors started.

⭐ **Verify against a known-good source before trusting it:** run it alongside
SatPC32's DDE feed on the same pass and compare the numbers. That is what
`pass-RS44-2026-08-29.csv` is for — 369 samples of an independently computed
pass to check against, including range rate crossing -4.09 to +4.11 km/s.

## Why bother at all

Nothing existing knows about *this* shack: AetherSDR on 4532, ShackBook for
logging, the K3NG rotator, keps that rot silently. Gpredict and SatPC32 are both
general-purpose tools with no Windows build and an awkward UI respectively.

The maths is borrowed. The value is the front end and the integration — which is
exactly the part that has to be built by whoever uses it.
