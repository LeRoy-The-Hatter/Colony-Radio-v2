import socket
import threading
from .config_io import load_user_config, save_user_config

import json
import re

def parse_steam_input(text: str) -> dict:
    """
    Accepts:
      - Full steamcommunity profile URL (profiles/<steamid64>) or (id/<vanity>)
      - Raw 64-bit SteamID
      - (Optional) vanity string (letters/numbers/underscore)
    Returns dict: {"type": "steamid64"|"vanity", "value": str}
    NOTE: Vanity resolution should be handled server-side (we just transmit).
    """
    s = (text or "").strip()
    if not s:
        return {"type": "invalid", "value": ""}

    # If it's a URL, try to pull out /profiles/<id> or /id/<vanity>
    m = re.search(r"steamcommunity\.com/(profiles|id)/([^/?#]+)", s, re.IGNORECASE)
    if m:
        kind = m.group(1).lower()
        token = m.group(2)
        if kind == "profiles" and token.isdigit():
            return {"type": "steamid64", "value": token}
        else:
            return {"type": "vanity", "value": token}

    # Raw 64-bit steamid64
    if s.isdigit() and len(s) >= 16:
        return {"type": "steamid64", "value": s}

    # Otherwise treat as vanity (conservative filter)
    if re.match(r"^[A-Za-z0-9_\-\.]{2,64}$", s):
        return {"type": "vanity", "value": s}

    return {"type": "invalid", "value": s}

class SteamStatusResult:
    def __init__(self, ok=False, reason="", online=False, display_name="", steamid64=""):
        self.ok = ok
        self.reason = reason
        self.online = online
        self.display_name = display_name
        self.steamid64 = steamid64

    @classmethod
    def from_json(cls, obj):
        return cls(
            ok=bool(obj.get("ok")),
            reason=str(obj.get("reason","")),
            online=bool(obj.get("online")),
            display_name=str(obj.get("display_name","")),
            steamid64=str(obj.get("steamid64","")),
        )

def _send_json_and_recv(sock, payload: dict, timeout=2.5):
    try:
        sock.settimeout(timeout)
        data = (json.dumps(payload) + "\n").encode("utf-8")
        sock.sendall(data)
        # Read one line
        buf = b""
        while True:
            ch = sock.recv(1)
            if not ch:
                break
            if ch == b"\n":
                break
            buf += ch
        if buf:
            obj = json.loads(buf.decode("utf-8", errors="ignore"))
            return obj
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": "No response"}


class Connector:
    def __init__(self, app_ref):
        self.app = app_ref
        self.sock = None

    def connect_async(self, ip: str, port: int):
        self.app.status_text.set(f"Connecting to {ip}:{port}…")
        threading.Thread(target=self._worker, args=(ip, port), daemon=True).start()

    def disconnect(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        self.app.connected.set(False)
        self.app._update_connection_indicator()
        self.app.status_text.set("Disconnected.")

    def _worker(self, ip, port):
        ok, err = False, ""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((ip, port))
            ok = True
        except Exception as e:
            err = str(e)
        finally:
            if not ok and s:
                try: s.close()
                except Exception: pass

        def done():
            if ok:
                self.sock = s
                self.app.connected.set(True)
                self.app._update_connection_indicator()
                self.app.status_text.set(f"Connected to {ip}:{port}")
                data = load_user_config()
                data.setdefault("server", {})["ip"] = ip
                data["server"]["port"] = str(port)
                save_user_config(data)
            else:
                self.app.connected.set(False)
                self.app._update_connection_indicator()
                self.app.status_text.set(f"Failed to connect to {ip}:{port} — {err or 'No response'}")
        self.app.root.after(0, done)


    def steam_check_status(self, steam_text: str):
        """Parse steam input and ask the server for status of that user.
        Protocol (JSON lines):
          Client -> {"op":"steam_status","id_type":"steamid64|vanity","id_value":"..."}
          Server -> {"ok":true, "online":true/false, "display_name":"...", "steamid64":"..."}
        """
        parsed = parse_steam_input(steam_text)
        if parsed["type"] == "invalid":
            self.app.steam_status_var.set("Invalid Steam input.")
            return

        if not self.sock:
            self.app.steam_status_var.set("Not connected to server.")
            return

        self.app.steam_status_var.set("Checking…")
        def worker():
            req = {"op": "steam_status", "id_type": parsed["type"], "id_value": parsed["value"]}
            resp = _send_json_and_recv(self.sock, req, timeout=3.0)
            try:
                res = SteamStatusResult.from_json(resp)
            except Exception:
                res = SteamStatusResult(ok=False, reason="Bad response")
            def done():
                if res.ok:
                    msg = f"{'ONLINE' if res.online else 'OFFLINE'}"
                    if res.display_name:
                        msg += f" — {res.display_name}"
                    if res.steamid64:
                        msg += f" (ID: {res.steamid64})"
                    self.app.steam_status_var.set(msg)
                    # Build a profile URL if we can and set the link on the app
                    profile_url = ""
                    if res.steamid64:
                        profile_url = f"https://steamcommunity.com/profiles/{res.steamid64}"
                    else:
                        # If server didn't resolve, fall back to vanity from parsed input
                        if parsed.get("type") == "vanity":
                            profile_url = f"https://steamcommunity.com/id/{parsed.get('value','')}"
                    if (res.display_name or parsed.get('value')) and profile_url:
                        name = res.display_name or parsed.get('value')
                        try:
                            self.app._set_steam_link(name, profile_url)
                        except Exception:
                            pass
                else:
                    self.app.steam_status_var.set(res.reason or "Not found")
            self.app.root.after(0, done)
        import threading
        threading.Thread(target=worker, daemon=True).start()
