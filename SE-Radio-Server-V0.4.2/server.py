from __future__ import annotations

import json
import socket
import struct
import time
import os
import hashlib
import threading
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Tuple

from udp_protocol import (
    VER,
    MT_AUDIO,
    MT_CTRL,
    CTRL_REGISTER,
    CTRL_HEARTBEAT,
    CTRL_PTT,
    CTRL_CHAN_UPD,
    CTRL_POSITION,
    CTRL_PRESENCE,
    CTRL_ADMIN_NET_MERGE,
    CTRL_ADMIN_NET_AUTOMERGE,
    CTRL_ADMIN_NET_UNMERGE_ALL,
    CTRL_UPDATE_OFFER,
    CTRL_UPDATE_RESPONSE,
    UPDATE_HTTP_PORT,
    HDR_SZ,
    AUDIO_HDR_SZ,
    CTRL_HDR_FMT,
    CTRL_HDR_SZ,
    AUDIO_FLAG_PTT,
    pack_hdr,
    unpack_hdr,
    now_ts48,
    SeqGen,
)

from session_mgr import SessionManager


def _app_base_dir() -> str:
    """Return the directory where runtime artifacts should live.

    When frozen into an executable (PyInstaller), __file__ points into a
    temp unpack directory. In that case, keep logs/updates next to the
    executable so they persist across runs.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_base_dir()
UDP_HOST = "0.0.0.0"
UDP_PORT = 8765
LOG_INTERVAL = 5.0  # seconds between summary logs
UPDATE_DIR = os.path.join(APP_DIR, "updates")
SERVER_LOG_PATH = os.path.join(APP_DIR, "server.log")
DEBUG_LOG = os.environ.get("RADIO_DEBUG", "1") not in ("", "0", "false", "False")
LOG_FILE_LOCK = threading.Lock()


class _LogFileTee:
    """Tee console output to a file while keeping the original stream behavior."""

    def __init__(self, target, path: str, lock: threading.Lock):
        self.target = target
        self.path = path
        self.lock = lock
        self.encoding = getattr(target, "encoding", "utf-8")
        self.errors = getattr(target, "errors", "replace")

    def write(self, data) -> None:
        try:
            if self.target:
                self.target.write(data)
        except Exception:
            pass
        try:
            if isinstance(data, (bytes, bytearray)):
                text = bytes(data).decode("utf-8", "replace")
            else:
                text = str(data)
        except Exception:
            try:
                text = repr(data)
            except Exception:
                text = ""
        if not text:
            return
        try:
            with self.lock:
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(text)
        except Exception:
            pass

    def flush(self) -> None:
        try:
            if self.target and hasattr(self.target, "flush"):
                self.target.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        try:
            return getattr(self.target, name)
        except Exception:
            raise AttributeError(name)


class QuietHTTPServer(ThreadingHTTPServer):
    """HTTP server that squelches noisy disconnect tracebacks."""

    def handle_error(self, request, client_address):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        return super().handle_error(request, client_address)


class UpdateManager:
    """Owns the update payload and exposes a tiny HTTP server for upload/download."""

    def __init__(self, base_dir: str, http_port: int, public_host: str, on_new_update=None) -> None:
        self.base_dir = base_dir
        self.http_port = int(http_port)
        self.public_host = public_host or "127.0.0.1"
        self._info = None
        self._file_path = None
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._on_new_update = on_new_update

    # --------------- lifecycle ---------------

    def start(self) -> None:
        """Start the embedded HTTP server for update upload/download."""
        os.makedirs(self.base_dir, exist_ok=True)
        Handler = self._make_handler()
        try:
            self._httpd = QuietHTTPServer(("0.0.0.0", self.http_port), Handler)
        except Exception as e:
            print(f"[SERVER][UPDATE] failed to start HTTP server on :{self.http_port}: {e}")
            self._httpd = None
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        print(f"[SERVER][UPDATE] HTTP host listening on 0.0.0.0:{self.http_port}")

    def stop(self) -> None:
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass

    # --------------- state helpers ---------------

    def set_update_bytes(self, filename: str, data: bytes) -> None:
        """Persist an uploaded update payload and compute metadata."""
        if not data:
            return
        fname = filename or "client_update.exe"
        safe_name = os.path.basename(fname)
        path = os.path.join(self.base_dir, safe_name)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[SERVER][UPDATE] write error for {path}: {e}")
            return

        sha = hashlib.sha256(data).hexdigest()
        info = {
            "name": safe_name,
            "size": len(data),
            "sha256": sha,
            "uploaded_at": time.time(),
        }
        with self._lock:
            self._info = info
            self._file_path = path
        print(f"[SERVER][UPDATE] new payload stored at {path} ({len(data)} bytes)")
        if callable(self._on_new_update):
            try:
                self._on_new_update(dict(info))
            except Exception as e:
                print(f"[SERVER][UPDATE] on_new_update callback error: {e}")

    def current_offer(self):
        """Return metadata dict (with download URL) if an update is available."""
        with self._lock:
            if not self._info or not self._file_path or not os.path.isfile(self._file_path):
                return None
            info = dict(self._info)
            path = self._file_path
        info["url"] = f"http://{self.public_host}:{self.http_port}/download"
        info["path"] = path
        return info

    # --------------- HTTP handlers ---------------

    def _make_handler(self):
        mgr = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # Keep the console clean; admin app already gets status.
                return

            def _send_json(self, obj, status: int = 200):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except Exception:
                    pass

            def do_GET(self):
                if self.path.startswith("/manifest"):
                    info = mgr.current_offer()
                    if not info:
                        return self._send_json({"ok": False, "reason": "no update"}, 404)
                    # Do not leak absolute path in manifest
                    payload = {k: v for k, v in info.items() if k != "path"}
                    return self._send_json({"ok": True, "update": payload})
                if self.path.startswith("/download"):
                    return mgr._serve_file(self)
                return self._send_json({"ok": False, "reason": "unknown path"}, 404)

            def do_POST(self):
                if self.path.startswith("/upload"):
                    return mgr._handle_upload(self)
                return self._send_json({"ok": False, "reason": "unknown path"}, 404)

        return Handler

    def _handle_upload(self, handler):
        length = 0
        try:
            length = int(handler.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        name = handler.headers.get("X-Filename", "client_update.exe")
        if length <= 0:
            return handler._send_json({"ok": False, "reason": "empty body"}, 400)
        try:
            data = handler.rfile.read(length)
        except Exception as e:
            return handler._send_json({"ok": False, "reason": f"read error: {e}"}, 400)
        self.set_update_bytes(name, data)
        info = self.current_offer()
        payload = {k: v for k, v in info.items() if k != "path"} if info else {}
        return handler._send_json({"ok": True, "update": payload})

    def _serve_file(self, handler):
        info = self.current_offer()
        if not info:
            return handler._send_json({"ok": False, "reason": "no update"}, 404)
        path = info.get("path")
        if not path or not os.path.isfile(path):
            return handler._send_json({"ok": False, "reason": "missing file"}, 404)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            return handler._send_json({"ok": False, "reason": f"read error: {e}"}, 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        handler.end_headers()
        try:
            handler.wfile.write(data)
        except Exception:
            pass



class UdpServer(object):
    def _log(self, msg: str) -> None:
        if DEBUG_LOG:
            try:
                print(msg)
            except Exception:
                pass

    def _resolve_advertise_host(self, host: str) -> str:
        """Choose an address to hand to clients for HTTP update downloads."""
        env_host = (os.environ.get("UPDATE_ADVERTISE_HOST") or os.environ.get("PUBLIC_HOST") or "").strip()
        if env_host:
            return env_host
        h = (host or "").strip()
        if h and h != "0.0.0.0":
            return h
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                cand = s.getsockname()[0]
                if cand and not cand.startswith("127."):
                    return cand
        except Exception:
            pass
        try:
            cand = socket.gethostbyname(socket.gethostname())
            if cand and not cand.startswith("127."):
                return cand
        except Exception:
            return "127.0.0.1"
        return "127.0.0.1"

    def _on_new_update(self, info: dict) -> None:
        """Callback from UpdateManager when a new payload is uploaded."""
        # Reset offers so everyone can be notified again.
        self._update_offered = {}
        # Immediately notify currently active sessions.
        try:
            enriched = self.update_mgr.current_offer() if getattr(self, "update_mgr", None) else info
            self._broadcast_update_offer(enriched or info)
        except Exception as e:
            print(f"[SERVER][UPDATE] broadcast error: {e}")

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))
        self.sock.setblocking(True)

        self.mgr = SessionManager()
        self.seq = SeqGen()
        self._last_log = time.time()
        self._running = True
        self.advertise_host = self._resolve_advertise_host(host)
        self.update_http_port = UPDATE_HTTP_PORT
        self.update_mgr = UpdateManager(UPDATE_DIR, self.update_http_port, self.advertise_host, on_new_update=self._on_new_update)
        self.update_mgr.start()
        self._update_offered = {}  # ssrc -> last sha/ts we offered

        print(f"[SERVER] UDP listening on {self.host}:{self.port}")
        print(f"[SERVER][UPDATE] downloads served from http://{self.advertise_host}:{self.update_http_port}/download")

    # ---------------- public API ----------------

    def close(self) -> None:
        self._running = False
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            if hasattr(self, "update_mgr") and self.update_mgr:
                self.update_mgr.stop()
        except Exception:
            pass

    def serve_forever(self) -> None:
        # Blocking server loop.
        while self._running:
            try:
                pkt, addr = self.sock.recvfrom(65535)
            except OSError:
                # Socket closed
                break
            except Exception:
                continue

            if len(pkt) < HDR_SZ:
                continue

            try:
                v, mtype, seq, ts, ssrc = unpack_hdr(pkt)
            except struct.error:
                continue

            body = pkt[HDR_SZ:]

            if mtype == MT_CTRL:
                self._handle_ctrl(addr, ssrc, body)
            elif mtype == MT_AUDIO:
                self._handle_audio(ssrc, body)
            # MT_ACK currently ignored

            self._maybe_log()

    # ---------------- handlers ----------------

    def _handle_audio(self, ssrc: int, payload: bytes) -> None:
        # Handle MT_AUDIO packets: [flags:1][len:2][opus_data...]
        if len(payload) < 3:
            return

        try:
            flags, length = struct.unpack("!BH", payload[:3])
        except struct.error:
            return

        if length <= 0 or len(payload) < 3 + length:
            return

        data = payload[3 : 3 + length]
        if not data:
            return
        # Log the first audio frame per SSRC so we know audio is flowing.
        try:
            if getattr(self, "_seen_audio_ssrc", None) is None:
                self._seen_audio_ssrc = set()
            if ssrc not in self._seen_audio_ssrc:
                self._seen_audio_ssrc.add(ssrc)
                self._log(f"[AUDIO][RX] first audio frame from ssrc={ssrc} bytes={len(data)}")
        except Exception:
            pass

        # Bookkeeping / stats
        try:
            self.mgr.note_audio_for(ssrc, len(data))
        except Exception:
            # Don't let bookkeeping failures kill the server
            pass

        # Determine recipients based on active network on the server side.
        active_net = ""
        sender_chan_idx = 0
        chan_net = ""
        recipients = []
        try:
            recipients, active_net, sender_chan_idx, chan_net = self.mgr.audio_recipients_for(ssrc)
        except Exception:
            recipients, active_net, sender_chan_idx, chan_net = [], "", 0, ""

        # Build a human-readable recipient list for logging.
        rec_strs = []
        for s, rx_idx in recipients:
            try:
                r_ssrc = getattr(s, "ssrc", None)
                r_addr = getattr(s, "addr", None)
                if r_addr and len(r_addr) == 2:
                    rec_strs.append(f"{r_ssrc}@{r_addr[0]}:{r_addr[1]}/ch{rx_idx}")
                else:
                    rec_strs.append(f"{r_ssrc}/ch{rx_idx}")
            except Exception:
                continue

        net_label = chan_net or active_net or "NONE"
        print(f"[SERVER][AUDIO] from ssrc={ssrc} chan={sender_chan_idx} net={net_label} recipients=[{', '.join(rec_strs)}]")

        if not recipients:
            # Nothing to forward to for this network.
            return

        # Rebuild and send MT_AUDIO for each recipient so we can embed the
        # receiver's channel index in the high nibble of the flags.
        try:
            seq = self.seq.next()
        except Exception:
            seq = 0
        ts48 = now_ts48()

        for s, rx_idx in recipients:
            try:
                chan_bits = (int(rx_idx) & 0x03) << 4
                flags_out = (flags & 0x0F) | chan_bits
                body = struct.pack("!BH", flags_out, len(data)) + data
                hdr = pack_hdr(VER, MT_AUDIO, seq, ts48, int(ssrc))
                pkt = hdr + body
                self.sock.sendto(pkt, s.addr)
            except Exception:
                # Ignore per-recipient send failures
                continue

    def _offer_update_if_any(self, addr: Tuple[str, int], ssrc: int, info_override=None, force: bool = False) -> None:
        """Send an update offer to this client if a payload is available."""
        if not getattr(self, "update_mgr", None):
            return
        try:
            key = int(ssrc)
        except Exception:
            key = None

        info = info_override or self.update_mgr.current_offer()
        if not info:
            return

        version_tag = info.get("sha256") or info.get("uploaded_at") or time.time()
        if not force and key is not None and self._update_offered.get(key) == version_tag:
            return

        payload = {k: v for k, v in info.items() if k != "path"}
        try:
            body = json.dumps(payload).encode("utf-8")
            hdr = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), 0)
            ctrl_hdr = struct.pack("!BH", CTRL_UPDATE_OFFER, len(body))
            pkt = hdr + ctrl_hdr + body
            self.sock.sendto(pkt, addr)
            if key is not None:
                self._update_offered[key] = version_tag
            print(f"[SERVER][UPDATE] offered {payload.get('name','update')} to {addr[0]}:{addr[1]}")
        except Exception as e:
            print(f"[SERVER][UPDATE] offer error: {e}")

    def _broadcast_update_offer(self, info: dict | None = None) -> None:
        """Push an update offer to all currently active sessions."""
        info = info or (self.update_mgr.current_offer() if getattr(self, "update_mgr", None) else None)
        if not info:
            return
        now = time.time()
        ACTIVE_TIMEOUT = 15.0
        for s in list(self.mgr.by_ssrc.values()):
            try:
                if int(getattr(s, "ssrc", 0)) == 0:
                    continue
                last = float(getattr(s, "last_seen", 0.0))
                if now - last > ACTIVE_TIMEOUT:
                    continue
                addr = getattr(s, "addr", None)
                if not addr or len(addr) != 2:
                    continue
                self._offer_update_if_any(addr, getattr(s, "ssrc", 0), info_override=info, force=True)
            except Exception:
                continue

    def _handle_ctrl(self, addr: Tuple[str, int], ssrc: int, payload: bytes) -> None:
        # Handle MT_CTRL packets.
        # Client sends: [code:1][length:2][JSON payload (length bytes)]
        if len(payload) < 3:
            return

        try:
            code, length = struct.unpack("!BH", payload[:3])
        except struct.error:
            return

        if length < 0 or len(payload) < 3 + length:
            body = b""
        else:
            body = payload[3 : 3 + length]

        self._log(f"[CTRL][RX] addr={addr} ssrc={ssrc} code={code} len={len(body)}")

        # Make sure the session exists / is updated for this sender
        try:
            self.mgr.upsert(addr, ssrc)
        except Exception:
            pass

        def _decode_json(b: bytes):
            if not b:
                return {}
            try:
                return json.loads(b.decode("utf-8", errors="ignore"))
            except Exception:
                return {}

        def _offer_update():
            try:
                self._offer_update_if_any(addr, ssrc)
            except Exception:
                pass

        if code == CTRL_REGISTER:
            info = _decode_json(body)
            # Use REGISTER payload to update/create the session with metadata
            try:
                if isinstance(info, dict):
                    self.mgr.upsert(addr, ssrc, **info)
                    self._log(f"[CTRL][REGISTER] addr={addr} ssrc={ssrc} info={info}")
                else:
                    self.mgr.upsert(addr, ssrc)
            except Exception:
                pass
            _offer_update()

        elif code == CTRL_HEARTBEAT:
            if hasattr(self.mgr, "note_heartbeat"):
                try:
                    self.mgr.note_heartbeat(ssrc)
                except Exception:
                    pass

        elif code == CTRL_PTT:
            info = _decode_json(body)
            ptt = bool(info.get("ptt", False))
            if hasattr(self.mgr, "note_ptt"):
                try:
                    self.mgr.note_ptt(ssrc, ptt)
                except Exception:
                    pass

        elif code == CTRL_CHAN_UPD:
            upd = _decode_json(body)
            if hasattr(self.mgr, "note_chan_update"):
                try:
                    self.mgr.note_chan_update(ssrc, upd)
                except Exception:
                    pass

        elif code == CTRL_POSITION:
            pos = _decode_json(body)
            # Ensure we have a session to attach the position to (e.g., Torch antenna snapshots).
            if hasattr(self.mgr, "upsert"):
                try:
                    self.mgr.upsert(addr, ssrc)
                except Exception:
                    pass
            if hasattr(self.mgr, "note_position"):
                try:
                    self.mgr.note_position(ssrc, pos)
                    self._log(f"[CTRL][POSITION] ssrc={ssrc} pos={pos}")
                except Exception:
                    pass

        elif code == CTRL_PRESENCE:
            # Presence can be used in two ways:
            #  1) From regular clients to update metadata (optional JSON body).
            #  2) From the admin_app.py, which sends an empty body as a poll
            #     and expects a roster JSON reply with presence_snapshot().
            info = _decode_json(body)
            if info and hasattr(self.mgr, "note_presence"):
                try:
                    self.mgr.note_presence(ssrc, info)
                except Exception:
                    pass

            # If this is an admin presence poll (no body, ssrc usually 0),
            # reply with a control frame containing the current roster.
            try:
                rows = []
                if hasattr(self.mgr, "presence_snapshot"):
                    rows = self.mgr.presence_snapshot()
                self._log(f"[CTRL][PRESENCE] poll from {addr} rows={len(rows)}")
                meta = {
                    "auto_merge_by_freq": bool(getattr(self.mgr, "auto_merge_by_freq", False)),
                    "manual_merge_count": len(getattr(self.mgr, "net_alias", {}) or {}),
                }
                payload = json.dumps({"ok": True, "rows": rows, **meta}).encode("utf-8")
            except Exception:
                payload = json.dumps({"ok": False, "rows": []}).encode("utf-8")

            try:
                # Build MT_CTRL header and a small CTRL subheader that matches
                # what admin_app.py expects: "!BBH" (code, reserved, length).
                hdr = pack_hdr(VER, MT_CTRL, 0, now_ts48(), 0)
                sub = struct.pack("!BBH", CTRL_PRESENCE, 0, len(payload))
                pkt = hdr + sub + payload
                self.sock.sendto(pkt, addr)
            except Exception:
                pass

        elif code == CTRL_ADMIN_NET_MERGE:
            # Admin requested that two network IDs be merged server-side.
            info = _decode_json(body)
            src = (info.get("from") or "").strip()
            dst = (info.get("into") or "").strip()
            if src and dst and hasattr(self.mgr, "merge_net"):
                try:
                    self.mgr.merge_net(src, dst)
                    canon_src = self.mgr.canonical_net(src)
                    canon_dst = self.mgr.canonical_net(dst)
                    print(f"[SERVER][NET] merge requested {src} -> {dst} (canon: {canon_src}, {canon_dst})")
                except Exception as e:
                    print(f"[SERVER][NET] merge error: {e!r}")

        elif code == CTRL_ADMIN_NET_AUTOMERGE:
            info = _decode_json(body)
            enabled = False
            try:
                enabled = bool(info.get("auto_merge", False))
            except Exception:
                enabled = False
            if hasattr(self.mgr, "set_auto_merge_by_freq"):
                try:
                    self.mgr.set_auto_merge_by_freq(enabled)
                    state = "ENABLED" if enabled else "DISABLED"
                    detail = "freq-only networking (ignores net headers)" if enabled else "network prefixes required"
                    print(f"[SERVER][NET] auto-merge-by-freq {state} :: {detail}")
                except Exception as e:
                    print(f"[SERVER][NET] auto-merge toggle error: {e!r}")

        elif code == CTRL_ADMIN_NET_UNMERGE_ALL:
            if hasattr(self.mgr, "reset_net_aliases"):
                try:
                    self.mgr.reset_net_aliases()
                    print("[SERVER][NET] cleared all manual network aliases")
                except Exception as e:
                    print(f"[SERVER][NET] unmerge-all error: {e!r}")

        elif code == CTRL_UPDATE_RESPONSE:
            resp = _decode_json(body)
            accept = bool(resp.get("accept"))
            reason = resp.get("reason", "")
            if accept:
                print(f"[SERVER][UPDATE] {addr} accepted update ({reason})")
            else:
                print(f"[SERVER][UPDATE] {addr} declined update ({reason})")
                # Remove the session so it no longer receives/forwards audio.
                try:
                    if hasattr(self.mgr, "drop"):
                        self.mgr.drop(ssrc)
                except Exception:
                    pass

    # ---------------- logging ----------------

    def _maybe_log(self) -> None:
        now = time.time()
        if now - self._last_log < LOG_INTERVAL:
            return
        self._last_log = now

        rows = None
        try:
            if hasattr(self.mgr, "summarize_frequencies"):
                rows = self.mgr.summarize_frequencies(top_n=6)
        except Exception:
            rows = None

        if not rows:
            print("[AUDIO] (no frames)")
            return

        print("[AUDIO]")
        for line in rows:
            try:
                print(line)
            except Exception:
                pass


def _init_logging() -> None:
    """Persist server stdout/stderr to server.log while keeping console output."""
    path = SERVER_LOG_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        sys.stdout = _LogFileTee(sys.stdout, path, LOG_FILE_LOCK)
    except Exception:
        pass
    try:
        sys.stderr = _LogFileTee(sys.stderr, path, LOG_FILE_LOCK)
    except Exception:
        pass


def main() -> None:
    _init_logging()
    srv = UdpServer(UDP_HOST, UDP_PORT)
    try:
        srv.serve_forever()
    finally:
        srv.close()


if __name__ == "__main__":
    main()
