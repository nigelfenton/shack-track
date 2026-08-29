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
