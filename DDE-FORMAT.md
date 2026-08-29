# SatPC32 DDE payload — observed format

Captured live 2026-08-28 from SatPC32 V.12.9, RS-44 selected.

    Server: SatPC32   Topic: SatPcDdeConv   Item: SatPcDdeItem     (updates ~1 Hz)

## The string

    SNRS-44 AZ19.3 EL-46.5 UP145966847 UMLSB DN435634488 DMUSB MA120.4 RR3.792985\r\n

Space-separated, each field carrying a two-letter prefix immediately followed by
its value — **no space between prefix and value**, so parse by prefix not by
position.

| Prefix | Meaning | Example | Notes |
|---|---|---|---|
| `SN` | satellite name | `RS-44` | may contain `-`; can contain spaces in some names |
| `AZ` | azimuth, degrees | `19.3` | |
| `EL` | elevation, degrees | `-46.5` | **negative below the horizon** |
| `UP` | uplink, **Hz** | `145966847` | Doppler-corrected, integer Hz |
| `UM` | uplink mode | `LSB` | |
| `DN` | downlink, **Hz** | `435634488` | Doppler-corrected, integer Hz |
| `DM` | downlink mode | `USB` | |
| `MA` | mean anomaly | `120.4` | |
| `RR` | range rate, km/s | `3.792985` | +ve receding, -ve approaching — the Doppler driver |

When no satellite is selected, or in in-range-only mode with the bird below the
horizon, the payload is instead:

    ** NO SATELLITE **\n\r\n

Note the terminator differs: `\n\r\n` there, plain `\r\n` on a data line.

## Settings that govern it

`DivOptions.SQF` in `%APPDATA%\SatPC32`, one switch per line. ⚠ **Line 1 is NOT
the DDE switch** — it governs 2nd-instance CAT/rotor steering. Cost a wasted
restart to learn.

    line 1   + / -   rotor+CAT steering available in a 2nd program instance
    line 2   + / -   DDE outputs CONSTANTLY (+) or only when elevation > 0 (-, default)
    line 3   + / -   frequencies exclude (+) or include (-) converter offsets
    line 4   + / -   steer downlink only (+) or downlink AND uplink (-, default)

All changes need a SatPC32 restart. For rotor control the author recommends
line 2 = `-`; `+` is useful for development so the feed publishes with nothing up.

## Reading it from Python

⭐ `import win32ui` **must come before** `import dde`, or pywin32 raises
*"This must be an MFC application - try 'import win32ui' first"*.

```python
import win32ui          # first, initialises MFC
import dde

srv = dde.CreateServer(); srv.Create("shacktrack")
conv = dde.CreateConversation(srv)
conv.ConnectTo("SatPC32", "SatPcDdeConv")     # "SatPC32ISS" for the ISS build
raw = conv.Request("SatPcDdeItem")
```

## Gotcha: the scope COM port

SatPC32 threw *"Could not open COM 38 for the Spectrum Scope"* at every start.
There is no COM 38 on aurora13 — the IC-9700 presents **COM19 and COM21**
(Silicon Labs CP210x), and the index drifts between sessions. Fixed by setting
line 1 of `Scope\ScopePar.SQF` to `21` (CI-V, 19200 baud, matching line 2).
Backup kept as `ScopePar.SQF.bak-20260828`.

---

## Drawing a pass arc — do not model it, fit it

Building the sky-plot arc from a hand-derived spherical model was a mistake worth
recording. A pass looks like it should fall out of a great-circle construction,
but it is not symmetric about its peak — the observer is on a rotating Earth —
and the model was **18.5° out in azimuth** at worst even after fixing an `asin`
quadrant bug with `atan2`.

Measured against a real RS-44 pass over II22TB (Skyfield, 41 samples, peak 41°):

| approach | max az error | max el error |
|---|---|---|
| hand-derived spherical model | **18.5°** | 4.3° |
| cubic polynomial in t | 5.81° | 6.24° |
| degree-5 polynomial | 2.24° | 2.29° |
| **degree-7 polynomial** | **0.95°** | **0.89°** |

So **16 coefficients** (8 az, 8 el, parameterised on t = 0..1 through the pass)
reproduce a pass to under a degree. Azimuth must be **unwrapped** past 360 first
or the fit tears at the wraparound.

That is the cheap way to carry a pass: compute once with Skyfield, store the
coefficients, evaluate anywhere — no propagator needed at draw time. The plot in
`design/interface-concepts.html` currently uses the 41 raw points directly
(exact, and simpler still when the data is already to hand).

`design/rs44-real-pass.txt` holds those samples, and
`~/bin/render-fd-sats.py` on shack-hub is the reference implementation — same
projection, `r = R*(90-el)/90`, north up.

## Options dialog (Setup -> Options)

Found while chasing an apparently satellite-centred map. **`Center Maps on` was
already set to `Observer`** -- the map only *looked* satellite-centred because
RS-44's footprint filled the frame from a sub-satellite point over central Asia,
half a world from II22TB (hence elevation -46.5).

Other settings in that dialog worth knowing:

- **`Automatic TX Stop after 60 sec`** -- SatPC32 has its own transmit timeout,
  a safety rail independent of anything AetherSDR does.
- **Orbit model SGP4/SDP4** -- the same propagator Skyfield uses, so SatPC32 and
  `render-fd-sats.py` agree.
- **`Rotor control` and `CAT control`** are both ticked to activate at start, so
  the GS-232 path is armed as soon as an interface is selected.

The dialog has both **OK** and **Store**; use Store to persist a change.

## Rotor setup — where it got to (2026-08-28)

⏳ **Not finished. The rotator was not connected**, so this was left partway.

`Setup -> Rotor setup` -> combo box -> **`Yaesu_GS-232`** is the right choice for a
K3NG (it speaks GS-232). Two things confuse this dialog:

- ⚠ The settings list shows **`LPT (1 - 4, only IF-100, FODTrack, RifPC)`**. That row
  applies ONLY to those three parallel-port interfaces and is **inert for GS-232**,
  as are the `Port address: $0278` and the LPT number. aurora13 has no LPT hardware
  at all. Only **Minimum elevation** and the H/V antenna corrections apply here.
- ⚠ The dialog has **two Store buttons**. The UPPER one saves the interface choice;
  the lower one saves the optional settings. `RotorInterface.SQF` still reads `0`,
  so the selection had NOT persisted -- check that file to confirm, do not trust the
  dialog's appearance.

⭐ **The COM port is NOT in this dialog.** It lives in `ServerSDX`, which SatPC32
launches automatically on restart and parks in the system tray: click it ->
**Setup** -> choose port and baud. The readme warns the port must not be the one
SatPC32 is using for CAT, or ServerSDX errors.

Blocker at the time: only COM19/COM21 existed and both are the IC-9700, so there was
no port for the rotator. Plug the K3NG in first, confirm a third COM appears, then
work through: select GS-232 -> upper Store -> restart SatPC32 -> set the port in
ServerSDX.

For LEO birds the author recommends **10 s intervals and 5°** rather than the
15 s/5° the dialog defaults to.

## Gpredict — what it actually is (checked 2026-08-29)

⚠ **Licence: GPL-2.0-OR-LATER**, not GPL-3. The source headers say "either
version 2 of the License, or (at your option) any later version". The "or later"
matters: it means code from it could be taken into a GPL-3 project such as
Aether-gate, which GPL-2-only would have forbidden. Talking to it over rigctl
creates no obligation at all — that is two programs on a protocol, not linkage.

⚠ **Actively maintained, but NOT for Windows.** Latest release **v2.6
(2026-08-16)**, with 2.4 / 2.5 / 2.5.1 / 2.5.2 before it — and **none ship a
Windows binary**. The last was `gpredict-win32-2.3.37.zip`, **January 2019**.
winget installs that 2019 build, which is what "actively maintained" hides.

Consequences of the 2019 build, both hit here:
- Its TLE update is **dead**: the bundled URLs are `celestrak.com/NORAD/elements/*.txt`,
  which 404 — the same rot that broke SatPC32. Verified all variants.
- Its bundled catalogue is from 2018 (ISS elements epoch `18020`) and **has no
  RS-44**, which launched Dec 2019. 78 amateur satellites were missing.

Both fixed by writing current elements straight into `~/Gpredict/satdata/*.sat`
(`TLE1=`/`TLE2=` lines) and creating `.sat` files for the missing ones. All six
tracked birds now read 0 days old. **This will need redoing** — Gpredict cannot
refresh itself.

Building 2.6 for Windows is possible (GTK-3 + autotools via MSYS2) but means
owning a build upstream abandoned five releases ago. Running **v2.6 on the Pi5**
is the better route to a current version: Linux is where it is maintained, and
it speaks rigctl over the network to AetherSDR on aurora13.

### The working CAT path (proven 2026-08-29)

⭐ **AetherSDR IS the rigctl server** — no Hamlib needed. Its CAT Control applet
has several ports; the enabled one is **4532** (Hamlib's default), dialect
`Rigctld`, VFO A. Verified live: `f` returned `145210000`, `m` returned
`FM / 15000`, and `dump_state` a full capability report.

Gpredict config is `~/Gpredict/hwconf/IC-9700.rig`: `Host=localhost Port=4532
Type=0 PTT=0` — Type=0 is RX-only. ⭐ Let Gpredict WRITE that file from its own
dialog; a hand-written `Type=1` was a guess and would have been wrong.

This path avoids everything that cost hours with SatPC32's CAT: no CI-V address,
no COM port, no baud rate.

## ✅ The CAT chain that works (proven 2026-08-29 01:35)

    Gpredict -> AetherSDR rigctl :4532 -> IC-9700 (network, RS-BA1)

Gpredict was engaged on the ISS and the radio moved from **145.210 to
145.800000 MHz** — confirmed by asking the radio itself through rigctl, not by
reading Gpredict's own display. That is the interaction SatPC32's CAT never
achieved despite a working COM port, and it needed **no CI-V address, no COM
port, no baud rate** — every obstacle from earlier in the evening bypassed.

⭐ shack-hub can reach `10.0.0.104:4532` across the LAN, so a future Shack-Track
server can live on the hub and still drive the radio on aurora13.

`rigctl_log.py` records the radio side: receive-only (`f` and `m` are read
commands that cannot change anything), one sample per second, CSV out. It is the
counterpart to `pass_log.py` — that one records what the software COMPUTED, this
one records what the radio DID. Neither proves the interaction alone.
