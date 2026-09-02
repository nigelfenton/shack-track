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
| **The interface** | Three concepts in `design/interface-concepts.html`, redrawn 29 Aug against **this** architecture — Skyfield for the maths, rigctl readback for the radio. |

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

⭐ **Verify against a known-good source before trusting it:** compare against the recorded pass rather than trusting the formula. That is what
`pass-RS44-2026-08-29.csv` is for — 369 samples of an independently computed
pass to check against, including range rate crossing -4.09 to +4.11 km/s.

## Why bother at all

Nothing existing knows about *this* shack: AetherSDR on 4532, ShackBook for
logging, the K3NG rotator, keps that rot silently. Gpredict and SatPC32 are both
general-purpose tools with no Windows build and an awkward UI respectively.

The maths is borrowed. The value is the front end and the integration — which is
exactly the part that has to be built by whoever uses it.

---

# Deployment — `g0jkn.com/shack-track`

The goal: show this at a club without carrying the shack there.

## Most of it already exists

⭐ **`g0jkn.com` already tunnels to the hub's Apache on port 80** (cloudflared,
active, with `g0jkn.com` / `www` / `shack.` all routed to `localhost:80`). So
`/shack-track` is a **vhost path, not new infrastructure** — no port forwarding,
no inbound hole, no new certificate.

The weekend-one server runs on the hub anyway, which is the same machine serving
the site. That makes deployment a `<Location>` block proxying to Flask.

⚠ **Four Apache modules are not enabled yet**: `proxy`, `proxy_http`,
`proxy_wstunnel` (needed for the 1 Hz live updates), `rewrite`. All ship with
Apache — `a2enmod` and a reload.

## The three concepts degrade differently when remote

| | Works remote? | Why |
|---|---|---|
| **B — tonight's passes** | ✅ fully | Pure computation from TLEs + QTH. No radio. **This is the demo-friendly one.** |
| **C — status strip** | ✅ mostly | Keps age is a hub-side check. Only the "radio" cell needs the shack. |
| **A — operating view** | ⚠ partly | Live az/el is fine, but the *radio readback* row needs rigctl on aurora13 — and the radio has to be **on**. |

For a club demo that split is probably fine: you are showing the interface and a
real pass, not necessarily a live radio at home at that moment. But **design A so
the radio row degrades honestly** — "radio not reachable" rather than a blank or,
worse, a stale number presented as current. That is the whole lesson of the
SatPC32 CAT evening.

## ⛔ Decide access before building the URLs

`g0jkn.com` is public. A page showing your station's **live frequency and where
your antenna is pointing** is more revealing than a projects page — it is
effectively a presence indicator for the house.

The pattern already exists here: the Field Day pages sit behind a club login.

Suggested split, to be confirmed:
- **public**: Concept B (passes) and the keps health — nothing station-specific
- **authenticated**: Concept A, the rotator bearing, and anything reading the radio

Getting this right early matters because it shapes the URL structure; retrofitting
auth onto one half of a single-page app is worse than planning two.

## Not in weekend one

Deployment is weekend **two or later**. Weekend one is `localhost` on the hub —
prove the pass list and the live view work before exposing anything.

# Future: an AetherSDR applet that opens Shack-Track

Nigel, 2026-09-02: *"would like this as an applet in AE that calls this up and opens the
app's web page."* Recorded here so it is not lost; not weekend-one or -two work.

Shape that fits AE's rules: a small applet (or a Tools-menu entry) that launches the
operator's browser at the Shack-Track URL with `QDesktopServices::openUrl`, the URL a
setting with a sensible default (`http://shack-hub:8781/`). Nothing satellite-specific
lives in AE; it stays a consumer of AE the way ShackBook and Shack-Bench are. Because it
adds a user-facing surface to AE it still needs an **RFC to Jeremy (@ten9876) first**
(GOVERNANCE.md: features need an RFC, bug fixes do not). Embedding the page inside AE
would mean QtWebEngine, which AE does not link; open-in-browser is the version to propose.

Prerequisite worth doing first: the hub-side page reachable by a stable name
(`shack-hub` resolves on the LAN; the g0jkn.com path for away-from-home).
