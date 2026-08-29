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
