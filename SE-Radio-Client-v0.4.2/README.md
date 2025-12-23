# Space Engineers Radio Client — Self-Test Build (v0.1)

This is a **standalone client** you can run on Windows to validate mic capture,
playback, push‑to‑talk, and the **edge‑of‑range radio effects** (hiss/crackle/cut‑outs)
*without* needing the server Relay yet.

> In this build, audio is local loopback for testing. You talk into your mic and hear the processed audio
> according to the "SQI" slider, which simulates being near or at the edge of antenna range.

## Features in v0.1
- Windows-friendly UI (Tkinter): input/output device pickers, Push‑To‑Talk, and SQI slider.
- 48 kHz, 20 ms frames, mono.
- Edge‑of‑range model with hysteresis and squelch tail (config baked in the code).
- Clean code split: `audio_io.py`, `effects.py`, `app.py` ready to extend with WebSocket + Opus.

## Install & Run (Windows)
1. Install Python 3.11+.
2. In a terminal:
   ```bat
   cd SE-Radio-Client-v0.1
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
3. Select your mic and speakers (or leave Default).
4. Click **Start Self-Test**.
5. Toggle **Push‑To‑Talk**, and move the **SQI** slider from 1.0 (clean) toward 0.0 (edge/out of range).

## What you're hearing
- As SQI drops:
  - Volume attenuates
  - Hiss bed increases
  - Random crackle bursts appear
  - Occasional frame drops produce natural "burble"
  - Below the squelch threshold, audio fades out with a short tail

This is the exact sound design we'll use when the server says you're near the edge of antenna range.

## Next steps (when we add the Relay)
- Add Opus encoding/decoding (Opus PLC will improve the "cut out" feel).
- Connect to the Relay via WebSocket; map server‑provided `SQI` per speaker→listener.
- Mix multiple talkers with capture effect and co‑channel interference rules.

## Notes
- Requires Windows audio drivers compatible with `sounddevice` (WASAPI/WDM/ASIO depending on your setup).
- If you don't hear anything, try different input/output devices in the dropdowns.
