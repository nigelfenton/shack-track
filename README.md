# Shack-Track

A better satellite operating **interface**, not another tracker. The orbital maths is
already solved by SatPC32; this is the operating surface that isn't there yet.

Status: **weekend one, item 1 + Concept B built 2026-09-02** and running on shack-hub as
`shack-track.service` at **http://10.0.0.51:8781/** (LAN only; the g0jkn.com path is not
wired up yet). `passes.py` (Skyfield engine), `server.py` (Flask, `/api/passes`,
`/api/keps`, `/api/health`), `static/index.html` (Concept B, live), `satellites.json`
(the birds), `test_passes.py` (run it after touching the engine). Reads
`/home/nigel/satpass/amateur.tle`, the file the daily FD renderer refreshes.

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

## Rotator: nothing to build

SatPC32 drives GS-232 rotators **natively** via its bundled `ServerSDX.exe`. Setup →
Rotor setup → `Yaesu_GS-232` → Store → restart. Nigel's K3NG fork speaks GS-232, so this
is a config job; direct RS-232 remains the fallback. Only *Minimum elevation* and the H/V
antenna corrections apply to GS-232 controllers.

## Scope

1. **Keps health watchdog** — build first. No hardware, no interface decisions, and it is
   the gap that actually bit: keps sat 7 months stale because Celestrak moved to
   `celestrak.org` and the old URLs 404'd silently. ⭐⭐ Count **TLE lines, never HTTP
   status** — Celestrak answers a bad GROUP with `200 OK` and an `Invalid query` body.
2. **Pass dashboard** — the actual deliverable. See `design/interface-concepts.html`.
3. ~~Rotator bridge~~ — cut, see above.

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

**Neither RS-44 capture in this folder is a clean reference.** Both have a note or a caveat:

| file | what it is |
|---|---|
| `rs44-capture-2026-09-02.csv` | the real 41° pass, but contaminated — logger started 25 min early on 2 m, and the operator hand-tuned against a recentring driver. Its own verdict says NOT PROVEN. See `rs44-capture-2026-09-02.NOTES.md`. |
| `pass-RS44-2026-08-29.csv` | despite the name, **not a pass at all** — 12 min of pre-AOS geometry, see below. |

## ⚠ About `pass-RS44-2026-08-29.csv`

Despite the name it is **not a pass**: 735 rows, 04:01–04:13Z, elevation −50° to −29°,
never above the horizon, `radio_hz` empty throughout. It is the twelve minutes of
`sat_capture.py` output *before* AOS. It is still useful for one thing — its `up_hz`/`dn_hz`
columns pin the Doppler sign convention (`test_passes.py` uses it for exactly that) — but
do not cite it as the RS-44 pass evidence. The proven live capture is the radio log
described in `DDE-FORMAT.md` (−1753 → −2538 Hz against Skyfield's −2166 Hz).
