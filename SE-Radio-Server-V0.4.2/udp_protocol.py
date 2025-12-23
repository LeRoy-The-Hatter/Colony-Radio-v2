from __future__ import annotations

import os
import struct
import time
import threading

# Protocol version
VER = 1

# Message types
MT_AUDIO = 0
MT_CTRL  = 1
MT_ACK   = 2

# Control codes (client <-> server)
CTRL_REGISTER, CTRL_HEARTBEAT, CTRL_PTT, CTRL_CHAN_UPD, CTRL_POSITION, CTRL_PRESENCE = (1, 2, 3, 4, 5, 6)

# Admin/control-plane extra codes (admin_app -> server only)
CTRL_ADMIN_NET_MERGE = 7
CTRL_ADMIN_NET_AUTOMERGE = 10
CTRL_ADMIN_NET_UNMERGE_ALL = 11

# Update workflow
# - Server -> Client: CTRL_UPDATE_OFFER with JSON payload describing the update.
# - Client -> Server: CTRL_UPDATE_RESPONSE with {"accept": true/false, "reason": "..."}
CTRL_UPDATE_OFFER = 8
CTRL_UPDATE_RESPONSE = 9

# HTTP port used by the server's built-in update file host/uploader.
# Allow override via environment so the HTTP port can be forwarded/exposed alongside UDP.
UPDATE_HTTP_PORT = int(os.environ.get("UPDATE_HTTP_PORT", "9876"))

# Audio flags
AUDIO_FLAG_PTT = 0x01  # bit 0 = PTT active
# Bit 1 signals that the payload is raw PCM (float32/int16). If clear, receivers may assume Opus.
AUDIO_FLAG_CODEC_PCM = 0x02
# Bit 2 (client hint) marks PCM as int16; if clear, PCM is float32. Server forwards flags unchanged.
AUDIO_FLAG_PCM_I16 = 0x04

# Common header: version, msg_type, seq, ts48, ssrc
HDR_FMT = "!BBHII"
HDR_SZ  = struct.calcsize(HDR_FMT)

# Audio header: flags, length (bytes of Opus payload)
AUDIO_HDR_FMT = "!BH"
AUDIO_HDR_SZ  = struct.calcsize(AUDIO_HDR_FMT)

# Control header for client->server payloads: code, length
# NOTE: This matches what the main server expects: "!BH"
CTRL_HDR_FMT = "!BH"
CTRL_HDR_SZ  = struct.calcsize(CTRL_HDR_FMT)


def pack_hdr(v: int, m: int, s: int, t: int, x: int) -> bytes:
    """Pack the fixed outer header."""
    return struct.pack(HDR_FMT, v, m & 0xFF, s & 0xFFFF, t & 0xFFFFFFFF, x & 0xFFFFFFFF)


def unpack_hdr(b: bytes):
    """Unpack the fixed outer header; returns (ver, msg_type, seq, ts48, ssrc)."""
    return struct.unpack(HDR_FMT, b[:HDR_SZ])


def now_ts48() -> int:
    """Return a monotonic 48 kHz sample counter as an integer.

    This is used as the timestamp field in the outer header.
    """
    if not hasattr(now_ts48, "_t0"):
        now_ts48._t0 = time.monotonic()
    return int((time.monotonic() - now_ts48._t0) * 48000)


class SeqGen(object):
    """Simple 16-bit sequence-number generator with a lock."""

    __slots__ = ("_seq", "_lock")

    def __init__(self, initial: int = 0) -> None:
        self._seq = initial & 0xFFFF
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFF
            return self._seq


# --- Compatibility wrapper for admin_app.py ---
def pack_ctrl_header(ctrl_type: int, seq: int, ssrc: int, ts: int | None = None) -> bytes:
    """Build the fixed MT_CTRL outer header + empty CTRL subheader.

    This is mainly used by admin_app.py when it wants to send a control
    frame with no JSON body. For non-empty bodies, callers should build:

        pack_hdr(..., MT_CTRL, ...) + struct.pack("!BH", code, len(payload)) + payload
    """
    if ts is None:
        ts = now_ts48()
    ctrl_hdr = struct.pack("!BH", ctrl_type & 0xFF, 0)
    return pack_hdr(VER, MT_CTRL, seq, ts, ssrc) + ctrl_hdr
# --- end compatibility wrapper ---
