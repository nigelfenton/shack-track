# Shack-Track

A better satellite operating **interface**, not another tracker. The orbital maths is
already solved by SatPC32; this is the operating surface that isn't there yet.

Status: **weekend one, item 1 + Concept B built 2026-09-02** and running on shack-hub as
`shack-track.service` at **http://10.0.0.51:8781/** (LAN only; the g0jkn.com path is not
wired up yet). `passes.py` (Skyfield engine), `server.py` (Flask, `/api/passes`,
`/api/keps`, `/api/health`), `static/index.html` (Concept B, live), `satellites.json`
(the birds), `test_passes.py` (run it after touching the engine). Reads
`/home/nigel/satpass/amateur.tle`, the file the daily FD renderer refreshes.

**Your QTH.** The `satellites.json` in this repo carries a placeholder QTH, the centre
of **II22TB** on Ascension Island. Copy it somewhere outside the checkout, put your own
`qth` in it, and point `SHACKTRACK_SATS` at the copy; `server.py` and `sat_capture.py`
both read it from there. The `.out` files are emptied placeholders for local capture logs.

**Setup page.** `/setup` edits that copy from a browser:
- location, as a grid square or latitude/longitude (each shows the other, so a typo is visible)
- height and minimum elevation
- the radio's rigctl address, with a **Test radio** button that only reads frequency and mode

A saved radio address (`"rigctl"` in the file) overrides `SHACKTRACK_RIGCTL`. Changes are live on
the next refresh, with no restart. Every field is validated before anything is written; the
previous file is kept as `.bak`.

⚠ The setup endpoints change the station and make the server connect to an address the caller
names, and the server has no auth of its own. Put `/setup` and `/api/setup` behind a login in front
of it (on the hub, Apache Basic Auth). `test_setup.py` covers the API.

## Why not build a tracker

SatPC32 (Erich Eichmann **DK1TB**, closed shareware, proceeds to AMSAT — registered here)
already does prediction, Doppler and rotator control. Re-implementing SGP4 would redo the
one part nobody needs redone.

It publishes everything needed over a **DDE** interface — the sanctioned integration path,
documented by the author, who ships demo clients in Visual Basic and Delphi:

    Server SatPC32 | Topic SatPcDdeConv | Item SatPcDdeItem     (SatPC32ISS for the ISS build)

Updated **once per second**. Carries satellite name, azimuth, elevation, and *both* uplink
and downlink frequencies with modes (`DivOptions.SQF` line 4 = `-` selects both). Sends the
literal `** No Satellite **` when nothing is up.

⚠ The exact string layout is **not yet verified** — it needs SatPC32 running against a live
pass. Every number in the mockups is plausible, not observed.

⚠ Keep the DDE reader **thin and swappable**. Delphi `.dpr`/`.dfm`/`.dcu` and a
`dproj.2007` date this interface to Borland-era tooling, and DDE itself shipped with
Windows 3.0 in 1990. It works and will keep working, but the code that speaks it will feel
archaeological — quarantine it in one module. Gpredict (GPL, speaks rotctld) is the
fallback if SatPC32 is ever abandoned.

## Rotator: a network rotator, armed by hand

The first plan (August 2026) was to build nothing: SatPC32 drives GS-232 rotators itself. That
stopped being true when Shack-Track took over the tracking, so `rotor.py` now drives a rotator
over the network with Hamlib's rotator protocol (`rotctld`, usually TCP 4533). That reaches a
real `rotctld` in front of any controller, the ESP32 and CSN boxes that speak it natively, and
`rotor_sim.py`.

- **Nothing moves until armed** on the operating page, and a restart never arms it.
- Armed, it follows whatever is being tracked: to the AOS bearing four minutes before a pass,
  on the bird during it, stopped at LOS.
- It re-commands only after the bird has moved 2 degrees.
- A rotator that is off target and not moving for 12 s is stopped, disarmed and called stalled.
- Setup has the lowest elevation to point at and the mount's elevation travel (0-90 or 0-180).
  With 0-180 a pass that crosses north is worked flipped; with 0-90 the page warns of the swing.

`rotor_sim.py` is a rotator controller with no rotator: K3NG's GS-232 emulation on one TCP
port and the rotctld protocol on another, one simulated mount behind both, with a `--stall`
option. Not handled yet: azimuth overlap (more than 360 degrees), a stop that is not at north,
parking. Not run on real hardware.

## Scope

1. **Keps health watchdog** — build first. No hardware, no interface decisions, and it is
   the gap that actually bit: keps sat 7 months stale because Celestrak moved to
   `celestrak.org` and the old URLs 404'd silently. ⭐⭐ Count **TLE lines, never HTTP
   status** — Celestrak answers a bad GROUP with `200 OK` and an `Invalid query` body.
2. **Pass dashboard** — the actual deliverable. See `design/interface-concepts.html`.
3. **Rotator** — cut in August, back in November 2026 as a network driver; see above.

**Out of scope:** SGP4, TLE propagation, pass prediction, transmit control. Not an
AetherSDR feature either — it *consumes* AE the way Shack-Bench does.

## Open question before any code

**Full duplex.** `SmartCatProtocol.cpp:242` has AetherSDR deliberately report satellite
mode OFF to Hamlib. A V/U bird needs uplink and downlink at once. Settle it with a
**receive-only** pass; if duplex does not work, Concept A's uplink row is showing something
the radio is not doing.

## design/interface-concepts.html

Three concepts, drawn in AetherSDR's real dark palette (values lifted from
`src/core/ThemeSeedGenerated.cpp`: `#0f0f1a` ground, `#00b4d8` accent, `#8ea8c0` secondary
text, `#4dd87a`/`#ffb84d`/`#ff4d4d` semantics) so the shack reads as one system.

- **A — Operating view.** Polar sky plot plus live telemetry, for the ninety seconds you
  are working the bird.
- **B — Tonight's passes.** Bar length = time above horizon, colour = elevation class.
- **C — Status strip.** One line for a second monitor; carries **keps age**, the thing you
  cannot see from the radio.

### ⭐ The sky plot, and the mistake worth not repeating

First version drew the pass arc **on** the outer circle. But that circle *is* the horizon
(elevation 0°), so the arc claimed the satellite skimmed the horizon for the whole pass
while the panel beside it said 61° maximum — picture and numbers contradicting each other.

Correct construction is a polar sky plot: **centre = zenith (90°), rim = horizon (0°)**,
so `r = R × (1 − el/90)`. The arc bows **inward**, and how close it comes to the centre
*is* the maximum elevation. That is what makes the plot earn its space — a high pass draws
a tight loop near the middle, a scrape hugs the rim, and the shape tells you pass quality
before you read a single number.

Second defect, caught on review: the "travelled" segment climbed to 44° then fell back to
34° while the panel said *rising*. Marker, path end, panel figures and the aria description
must all describe **one** instant — now az 132, el 44, still climbing toward a 61° peak.

## ⚠ About the capture files

**The raw captures are not published.** They were logged at the station, and a timed
az/el/range-rate record of a known satellite gives the observer's position away, so they
stay local. What they showed is recorded below and in the notes; the one test that needed
one keeps three rows inline.

**Neither RS-44 capture was a clean reference.** Both have a note or a caveat:

| file | what it is |
|---|---|
| `rs44-capture-2026-09-02.csv` | the real 41° pass, but contaminated — logger started 25 min early on 2 m, and the operator hand-tuned against a recentring driver. Its own verdict says NOT PROVEN. See `rs44-capture-2026-09-02.NOTES.md`. |
| `pass-RS44-2026-08-29.csv` | despite the name, **not a pass at all** — 12 min of pre-AOS geometry, see below. |

## ⚠ About `pass-RS44-2026-08-29.csv`

Despite the name it is **not a pass**: 735 rows, 04:01–04:13Z, elevation −50° to −29°,
never above the horizon, `radio_hz` empty throughout. It is the twelve minutes of
`sat_capture.py` output *before* AOS. It is still useful for one thing — its `up_hz`/`dn_hz`
columns pin the Doppler sign convention (`test_passes.py` keeps three of its rows inline for exactly that) — but
do not cite it as the RS-44 pass evidence. The proven live capture is the radio log
described in `DDE-FORMAT.md` (−1753 → −2538 Hz against Skyfield's −2166 Hz).

## Licence

GPL-3.0-or-later; see [`LICENSE`](LICENSE). Copyright (C) 2026 Nigel Fenton, G0JKN.
