# udp_client.py — sends audio ONLY when TX/PTT is active
from __future__ import annotations

import socket, threading, time, json, struct, random, os
from typing import Optional, Tuple, List, Callable

from udp_protocol import (
    VER, MT_AUDIO, MT_CTRL, MT_ACK,
    CTRL_REGISTER, CTRL_HEARTBEAT, CTRL_PTT, CTRL_CHAN_UPD, CTRL_POSITION, CTRL_PRESENCE,
    CTRL_UPDATE_RESPONSE,
    AUDIO_FLAG_PTT, AUDIO_FLAG_CODEC_PCM, AUDIO_FLAG_PCM_I16,
    HDR_SZ, AUDIO_HDR_SZ, CTRL_HDR_SZ,
    pack_hdr, unpack_hdr, now_ts48, SeqGen
)

from opus_shim import OpusShim


def _default_log(msg: str) -> None:
    print(msg)


class UdpVoiceClient:
    """
    Lightweight UDP voice client with:
    - Control packets (REGISTER, HEARTBEAT, PTT, CHAN_UPD, POSITION, PRESENCE)
    - Audio packets (Opus when available; PCM fallback when Opus payload is empty)
    - RX callback hooks for audio/control
    """

    def __init__(
        self,
        host: str,
        port: int,
        ssrc: Optional[int] = None,
        nick: str = "client",
        net: str = "NET-1",
        on_log: Optional[Callable[[str], None]] = None,
        client_id: Optional[str] = None,
    ) -> None:
        self.server = (host, int(port))
        self.nick = nick
        self.net = net
        self._log = on_log or _default_log

        # UDP socket
        self.server = (host, int(port))
        self.nick = nick
        self.net = net
        self._log = on_log or _default_log
        self.client_id = client_id
        # Opus-only: PCM fallback disabled.
        self.prefer_opus = True
        self._warned_opus_decode = False

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

        # Session info
        self.ssrc = ssrc if ssrc is not None else random.randint(1, 0xFFFFFFFF)
        self.seq = SeqGen()

        # Opus encoder/shim: 48 kHz, 10 ms frames -> 480 samples
        self.enc = OpusShim(rate=48000, channels=1, frame_ms=10)

        # Channel config: 4 channels A–D
        self.channels = {
            "A": {"freq": 100.0, "mode": "PTT"},
            "B": {"freq": 101.0, "mode": "PTT"},
            "C": {"freq": 102.0, "mode": "PTT"},
            "D": {"freq": 111.1, "mode": "PTT"},
        }
        self.active_tx = "A"
        self.monitor = {"A": True, "B": False, "C": False, "D": False}

        self._ptt = False
        self.run = False
        # When True, server is asked to route our own TX back to us (debug loopback).
        self.allow_loopback = False
        # RX stats for debugging jitter/ordering
        self._rx_stat_last_ts = None
        self._rx_stat_ctr = 0
        self._rx_stat_last_seq = {}

        # Callbacks
        self.on_rx_audio: Optional[Callable[[object, int], None]] = None
        self.on_rx_ctrl: Optional[Callable[[dict], None]] = None

        # Threads
        self._rx_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None

        self._log(f"[CLIENT][UDP] created UdpVoiceClient to {host}:{port} ssrc={self.ssrc}")

    @staticmethod
    def probe_server(host: str, port: int, timeout: float = 1.2) -> tuple[bool, str]:
        """Send a CTRL_PRESENCE poll and wait for a reply to confirm the server is reachable."""
        try:
            addr = (host, int(port))
        except Exception:
            return False, "Invalid host/port"

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            ctrl_hdr = struct.pack("!BH", CTRL_PRESENCE, 0)  # empty presence poll
            pkt = pack_hdr(VER, MT_CTRL, 0, now_ts48(), 0) + ctrl_hdr
            sock.sendto(pkt, addr)
            # Use a full UDP-sized buffer so large presence replies don't throw WSAEMSGSIZE (10040)
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return False, "No response from server"
        except Exception as e:
            return False, str(e)
        finally:
            sock.close()

        try:
            if len(data) < HDR_SZ + 1:
                return False, "Short response from server"
            _, mtype, _, _, _ = unpack_hdr(data)
            if mtype != MT_CTRL:
                return False, f"Unexpected message type {mtype}"
            ctrl_code = data[HDR_SZ]
            if ctrl_code != CTRL_PRESENCE:
                return False, f"Unexpected control code {ctrl_code}"
        except Exception as e:
            return False, f"Bad response: {e}"

        return True, ""

    # ---------------- Basic helpers ----------------

    def close(self) -> None:
        self.run = False
        try:
            self.sock.close()
        except Exception:
            pass

    # ---------------- Control packets ----------------

    def stop(self) -> None:
        """Alias for close(), used by the UI when reconnecting.
        Ensures background threads exit and the socket is closed."""
        self.close()

    def _send_register(self) -> None:
        """Initial registration: CTRL_REGISTER + basic client info."""
        data = {
            "nick": self.nick,
            "net": self.net,
            "ssrc": self.ssrc,
            "client_id": self.client_id if getattr(self, "client_id", None) is not None else None,
            "channels": self.channels,
            "monitor": self.monitor,
            "active_tx": self.active_tx,
            "loopback": bool(getattr(self, "allow_loopback", False)),
        }
        b = json.dumps(data).encode("utf-8")
        t = CTRL_REGISTER
        ctrl_hdr = struct.pack("!BH", t, len(b) & 0xFFFF)
        msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl_hdr + b
        self.sock.sendto(msg, self.server)
        self._log(f"[CLIENT][UDP][TX][CTRL] REGISTER len={len(b)}")

    def _send_heartbeat(self) -> None:
        t = CTRL_HEARTBEAT
        ctrl = struct.pack("!BH", t, 0)
        msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl
        try:
            self.sock.sendto(msg, self.server)
            self._log("[CLIENT][UDP][TX][CTRL] HEARTBEAT")
        except Exception:
            pass

    
    def _send_presence(self) -> None:
        """Send a presence update to the server.

        We include nick/net/client_id so the SessionManager.note_presence(...)
        hook can update the client's metadata (used by admin_app.py).
        """
        try:
            body = {
                "nick": getattr(self, "nick", "") or "",
                "net": getattr(self, "net", "") or "",
                "client_id": getattr(self, "client_id", None),
                "loopback": bool(getattr(self, "allow_loopback", False)),
            }
            b = json.dumps(body).encode("utf-8")
            t = CTRL_PRESENCE
            ctrl = struct.pack("!BH", t, len(b) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl + b
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] PRESENCE nick={body['nick']} net={body['net']} client_id={body['client_id']} loopback={body['loopback']}")
        except Exception:
            # Never let presence failures kill the client
            pass

    def update_client_id(self, new_client_id: Optional[str]) -> None:
        """Update our client_id (Steam ID) and push it to the server.

        This lets the UI change the Steam ID live without needing a full reconnect.
        The admin_app will pick up the new ID on the next presence poll.
        """
        # Normalise empty values to None
        cid = (str(new_client_id).strip() or None) if new_client_id is not None else None
        self.client_id = cid
        try:
            body = {
                "nick": getattr(self, "nick", "") or "",
                "net": getattr(self, "net", "") or "",
                "client_id": cid,
                "loopback": bool(getattr(self, "allow_loopback", False)),
            }
            b = json.dumps(body).encode("utf-8")
            t = CTRL_PRESENCE
            ctrl = struct.pack("!BH", t, len(b) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl + b
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] PRESENCE(update client_id={cid} loopback={body['loopback']}) len={len(b)}")
        except Exception:
            # Failing to send an update is non-fatal; the next REGISTER or presence will fix it.
            pass

    def send_update_response(self, accept: bool, reason: str = "") -> None:
        """Respond to a CTRL_UPDATE_OFFER from the server."""
        try:
            payload = json.dumps({"accept": bool(accept), "reason": reason}).encode("utf-8")
            ctrl_hdr = struct.pack("!BH", CTRL_UPDATE_RESPONSE, len(payload) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl_hdr + payload
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] UPDATE_RESPONSE accept={accept} reason={reason}")
        except Exception:
            pass

    def _hb_loop(self) -> None:
        # heartbeat + presence loop
        last_pres = 0.0
        while self.run:
            try:
                self._send_heartbeat()
                now = time.time()
                if now - last_pres > 5.0:
                    last_pres = now
                    self._send_presence()
            except Exception:
                pass
            time.sleep(1.0)

    def set_ptt(self, on: bool) -> None:
        """Set local PTT state and notify the server.

        The server's CTRL_PTT handler expects a JSON body like:
            { "ptt": true/false }

        It then calls SessionManager.note_ptt(ssrc, on) so the admin_app
        can show who is keyed up / transmitting.
        """
        self._ptt = bool(on)
        # Also tell server via CTRL_PTT
        try:
            body = json.dumps({"ptt": self._ptt}).encode("utf-8")
            ctrl_hdr = struct.pack("!BH", CTRL_PTT, len(body) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl_hdr + body
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] PTT={self._ptt} len={len(body)}")
        except Exception:
            pass

    def update_channels(self, state: dict) -> None:
        """Update channel frequencies/active/scan and notify server via CTRL_CHAN_UPD.
        
        The App passes in a dict like:
            {
                'active_channel': int,   # 0–3 for A–D
                'freqs': [fA, fB, fC, fD],
                'scan': bool,
                'scan_channels': [bool, bool, bool, bool],
            }
        
        The UDP server's SessionManager.note_chan_update() expects a JSON body:
            { 'active': int, 'freqs': [...], 'scan': bool, 'scan_channels': [...] }
        so we translate/normalize the keys here before sending CTRL_CHAN_UPD.
        """
        if not isinstance(state, dict):
            return
        try:
            active = int(state.get('active_channel', 0) or 0)
        except Exception:
            active = 0
        freqs = state.get('freqs')
        try:
            if isinstance(freqs, (list, tuple)):
                freqs_list = [float(x) for x in list(freqs)]
            else:
                freqs_list = [0.0, 0.0, 0.0, 0.0]
        except Exception:
            freqs_list = [0.0, 0.0, 0.0, 0.0]
        if len(freqs_list) < 4:
            freqs_list = (freqs_list + [0.0, 0.0, 0.0, 0.0])[:4]
        else:
            freqs_list = freqs_list[:4]
        scan = bool(state.get('scan', False))

        # Optional per-channel scan flags.
        scan_channels = state.get('scan_channels')
        try:
            if isinstance(scan_channels, (list, tuple)):
                sc = [bool(x) for x in scan_channels]
            else:
                sc = [False, False, False, False]
        except Exception:
            sc = [False, False, False, False]
        if len(sc) < 4:
            sc = (sc + [False, False, False, False])[:4]
        else:
            sc = sc[:4]

        # Keep a local mirror for any overlay / UI that inspects these.
        try:
            self.channels = {
                'A': {'freq': freqs_list[0], 'mode': 'PTT'},
                'B': {'freq': freqs_list[1], 'mode': 'PTT'},
                'C': {'freq': freqs_list[2], 'mode': 'PTT'},
                'D': {'freq': freqs_list[3], 'mode': 'PTT'},
            }
            self.scan_channels = sc
        except Exception:
            pass
        try:
            self.active_tx = ['A', 'B', 'C', 'D'][max(0, min(3, active))]
        except Exception:
            self.active_tx = 'A'

        try:
            payload = json.dumps({
                'active': int(active),
                'freqs': freqs_list,
                'scan': bool(scan),
                'scan_channels': sc,
            }).encode('utf-8')
            ctrl_hdr = struct.pack('!BH', CTRL_CHAN_UPD, len(payload) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl_hdr + payload
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] CHAN_UPD active={active} freqs={freqs_list} scan={scan} scan_channels={sc} len={len(payload)}")
        except Exception:
            pass

    def set_allow_loopback(self, enabled: bool) -> None:
        """Enable/disable server-routed loopback of our own TX back to us."""
        try:
            new_state = bool(enabled)
        except Exception:
            new_state = False
        changed = new_state != bool(getattr(self, "allow_loopback", False))
        self.allow_loopback = new_state
        if changed:
            try:
                self._send_presence()
            except Exception:
                pass

    def send_position(self, x: float, y: float, z: float) -> None:
        """Optional: 3D position for range/attenuation."""
        try:
            body = struct.pack("!fff", float(x), float(y), float(z))
            ctrl_hdr = struct.pack("!BH", CTRL_POSITION, len(body) & 0xFFFF)
            msg = pack_hdr(VER, MT_CTRL, self.seq.next(), now_ts48(), self.ssrc) + ctrl_hdr + body
            self.sock.sendto(msg, self.server)
            self._log(f"[CLIENT][UDP][TX][CTRL] POSITION ({x:.1f},{y:.1f},{z:.1f})")
        except Exception:
            pass

    # ---------------- Audio TX (PTT-gated) ----------------

    def send_audio(self, pcm_float32) -> None:
        """Encode and send only while PTT is ON. Opus-only; drops if encoder unavailable."""
        if not self._ptt:
            return

        # Normalize to mono float32
        try:
            import numpy as np  # already used in your project; local import to avoid hard dependency here
            buf = np.asarray(pcm_float32, dtype=np.float32)
            if hasattr(buf, 'ndim') and getattr(buf, 'ndim', 1) > 1:
                buf = buf.mean(axis=1)
            # Keep close to unity; very light clip guard only.
            buf = np.clip(buf * 0.99, -1.0, 1.0)
        except Exception:
            try:
                self._log("[CLIENT][UDP][TX] numpy unavailable; cannot send audio (Opus-only).")
            except Exception:
                pass
            return

        codec = "opus"
        flags = AUDIO_FLAG_PTT  # PTT is on

        use_opus = bool(getattr(self.enc, "enabled", False))
        if not use_opus:
            if not getattr(self, "_warned_no_opus", False):
                self._warned_no_opus = True
                self._log("[CLIENT][UDP][TX] Opus encoder unavailable; dropping audio (PCM disabled).")
            return

        try:
            data = self.enc.encode_float32(buf)
        except Exception:
            data = b""
        if not data:
            if not getattr(self, "_warned_opus_encode", False):
                self._warned_opus_encode = True
                self._log("[CLIENT][UDP][TX] Opus encode failed; dropping frame (PCM disabled).")
            return

        # 3) Build and send UDP packet
        aud_hdr = struct.pack('!BH', flags & 0xFF, len(data) & 0xFFFF)
        h = pack_hdr(VER, MT_AUDIO, self.seq.next(), now_ts48(), self.ssrc)

        try:
            self.sock.sendto(h + aud_hdr + data, self.server)
            # --- DEBUG LOG ---
            try:
                n = len(buf)
            except Exception:
                n = -1
            self._log(f"[CLIENT][UDP][TX] sent audio frame: {n} samples, {len(data)} bytes {codec}, PTT={self._ptt}")
        except Exception as e:
            try:
                self._log(f"[CLIENT][UDP][TX][ERROR] sendto failed: {e}")
            except Exception:
                pass

    def send_audio_frame_f32(self, pcm_float32) -> None:
        """Compatibility wrapper for GUI: forwards to send_audio."""
        self.send_audio(pcm_float32)

    # ---------------- RX loop ----------------
    def rx(self) -> None:
        while self.run:
            try:
                p, addr = self.sock.recvfrom(8192)
            except Exception:
                time.sleep(0.005)
                continue
            if not p or len(p) < HDR_SZ:
                continue

            try:
                ver, t, seq, ts, ssrc = unpack_hdr(p[:HDR_SZ])
            except Exception:
                continue
            if ver != VER:
                continue

            body = p[HDR_SZ:]

            if t == MT_AUDIO:
                if len(body) < AUDIO_HDR_SZ:
                    continue
                flags, sz = struct.unpack("!BH", body[:AUDIO_HDR_SZ])
                data = body[AUDIO_HDR_SZ:AUDIO_HDR_SZ + sz]
                if not data:
                    continue
                if ssrc == self.ssrc and not getattr(self, "allow_loopback", True):
                    continue
                try:
                    # Lightweight inter-arrival stats to debug jitter/ordering.
                    try:
                        import time as _time
                        now = _time.monotonic()
                        if self._rx_stat_last_ts is not None:
                            dt_ms = (now - self._rx_stat_last_ts) * 1000.0
                        else:
                            dt_ms = 0.0
                        self._rx_stat_last_ts = now
                        self._rx_stat_ctr += 1

                        last_seq = self._rx_stat_last_seq.get(ssrc)
                        seq_delta = (seq - last_seq) & 0xFFFF if last_seq is not None else 0
                        self._rx_stat_last_seq[ssrc] = seq
                        if self._rx_stat_ctr <= 120 or dt_ms > 40.0 or seq_delta not in (1, 0) or self._rx_stat_ctr % 30 == 0:
                            self._log(f"[CLIENT][UDP][RX][STAT] #{self._rx_stat_ctr} dt_ms={dt_ms:.1f} size={len(data)} flags=0x{flags:02X} seq={seq} dseq={seq_delta} ssrc={ssrc}")
                    except Exception:
                        pass

                    buf = None
                    codec = "opus"

                    # Decode Opus when present and not marked as PCM.
                    if not (flags & AUDIO_FLAG_CODEC_PCM) and getattr(self.enc, "enabled", False):
                        try:
                            try:
                                buf = self.enc.decode_to_float32(data, ssrc=ssrc)
                            except TypeError:
                                buf = self.enc.decode_to_float32(data)
                        except Exception:
                            buf = None

                    # PCM is intentionally disabled; drop with a warning once.
                    if buf is None and (flags & AUDIO_FLAG_CODEC_PCM):
                        if not getattr(self, "_warned_pcm_drop", False):
                            self._warned_pcm_drop = True
                            try:
                                self._log("[CLIENT][UDP][RX] dropped PCM frame (Opus-only mode).")
                            except Exception:
                                pass
                        continue

                    if buf is None:
                        if flags & AUDIO_FLAG_CODEC_PCM:
                            if not getattr(self, "_warned_pcm_drop", False):
                                self._warned_pcm_drop = True
                                try:
                                    self._log("[CLIENT][UDP][RX] dropped PCM frame (decode failed).")
                                except Exception:
                                    pass
                        elif not getattr(self, "_warned_opus_decode", False):
                            self._warned_opus_decode = True
                            try:
                                self._log("[CLIENT][UDP][RX] dropped audio frame: Opus decode failed and no PCM fallback.")
                            except Exception:
                                pass
                        continue

                    chan_idx = (int(flags) >> 4) & 0x03

                    try:
                        self._log(f"[CLIENT][UDP][RX] got audio frame from ssrc={ssrc}: {len(data)} bytes {codec}")
                    except Exception:
                        pass
                    if self.on_rx_audio:
                        try:
                            rate = getattr(self.enc, "rate", 48000) if codec == "opus" else 48000
                            try:
                                self.on_rx_audio(buf, rate, ssrc, chan_idx)
                            except TypeError:
                                try:
                                    self.on_rx_audio(buf, rate, ssrc)
                                except TypeError:
                                    # Back-compat: older callbacks take (buf, rate)
                                    self.on_rx_audio(buf, rate)
                        except Exception:
                            pass
                except Exception:
                    continue

            elif t == MT_CTRL:
                if len(body) < CTRL_HDR_SZ:
                    continue
                try:
                    ctype, clen = struct.unpack("!BH", body[:CTRL_HDR_SZ])
                except struct.error:
                    # Unknown or extended control header (e.g. admin roster); skip.
                    continue
                cdata = body[CTRL_HDR_SZ:CTRL_HDR_SZ + clen]
                if ctype == MT_ACK:
                    # ignore simple ACKs for now
                    continue
                # Other control (not strictly needed on client)
                if self.on_rx_ctrl:
                    try:
                        self.on_rx_ctrl({"type": ctype, "data": cdata, "ssrc": ssrc})
                    except Exception:
                        pass

    # ---------------- Lifecycle ----------------

    def start(self) -> None:
        """Start RX + heartbeat threads, send REGISTER."""
        if self.run:
            return
        self.run = True

        # Opus capability log (one-shot)
        try:
            self._log(f"[CLIENT][UDP][OPUS] encoder enabled={getattr(self.enc, 'enabled', False)} dll_hint={getattr(self.enc, 'dll', None)} err={getattr(self.enc, 'err', None)}")
        except Exception:
            pass

        # Send REGISTER immediately
        try:
            self._send_register()
        except Exception:
            pass

        # RX thread
        self._rx_thread = threading.Thread(target=self.rx, daemon=True)
        self._rx_thread.start()

        # Heartbeat thread
        self._hb_thread = threading.Thread(target=self._hb_loop, daemon=True)
        self._hb_thread.start()

        self._log(f"[CLIENT][UDP] started, server={self.server}, ssrc={self.ssrc}")
