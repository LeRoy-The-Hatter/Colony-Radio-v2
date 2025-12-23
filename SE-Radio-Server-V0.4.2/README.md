# SE-Radio-Server (MVP)

**Transport**: WebSocket (`ws://<server_ip>:8765/radio`)  
**Audio**: 48 kHz, mono, 20 ms frames (PCM16 little-endian).  
**Mixing**: Simple server-side mix per channel (can be toggled in `config.json`).

## Quick start

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

## Windows .exe build

- Run `.\build_exe.ps1` from PowerShell in this directory (adds PyInstaller to `.venv` and builds).
- Optional: `.\build_exe.ps1 -Clean` removes old `build/`, `dist/`, and `server.spec` before building.
- Output binary: `dist\SE-Radio-Server.exe` (double-click or launch from a shell).
- Logs (`server.log`) and update payloads (`updates/`) are kept next to the exe so they persist across runs.

## Message Protocol (JSON)

### Client → Server

- **auth**
```json
{"type":"auth","id":"<string>","display_name":"<string>","token":"<any>"}
```
(Dev mode accepts any token.)

- **join**
```json
{"type":"join","monitor_channels":["A","B"],"tx_channel":"A"}
```

- **presence**
```json
{"type":"presence","speaking":true,"tx":true,"tx_channel":"A"}
```

- **audio** (JSON fallback if you don't use binary frames)
```json
{"type":"audio","channel":"A","seq":123,"pcm16_le":"<base64>"}
```

Binary frames are also supported (more efficient). See `server.py` header comments.

### Server → Client

- **auth_ok**
```json
{"type":"auth_ok","you":{"id":"...","display_name":"..."}, "config":{...}}
```

- **roster**
```json
{"type":"roster","clients":[{"id":"...","display_name":"...","channel":"A","speaking":false}]}
```

- **audio**
```json
{"type":"audio","channel":"A","seq":456,"pcm16_le":"<base64>"}
```

## Paths

- WS endpoint: `/radio`
- Config: `config.json`

## Notes

- This is an MVP. It mixes audio per channel every 20 ms. If two talkers speak at once, their PCM is added with simple clipping.
- Upgrade path: add Opus + UDP for audio, keep WebSocket for signaling.

## Update downloads

- The built-in HTTP host that serves client updates listens on `UPDATE_HTTP_PORT` (default `9876`). You can override it via the environment: `UPDATE_HTTP_PORT=8765 python server.py`.
- Make sure the chosen port is reachable to clients over TCP (firewall/port-forward the port, not just UDP).
- If the server binds to `0.0.0.0` or reports a private address, set `UPDATE_ADVERTISE_HOST` (or `PUBLIC_HOST`) to the public IP/hostname you want clients to use, e.g. `UPDATE_ADVERTISE_HOST=radio.example.com`.
