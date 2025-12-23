
# opus_shim.py — tolerant codec shim for our UDP audio (v5)
# Provides BOTH names for compatibility:
#   - OpusDecoder
#   - OpusShim  (alias)
#
# Roles:
#  * Decoder: decode_to_float32(data) -> np.float32 mono in [-1, 1]
#  * Encoder: encode_float32(pcm) / encode_int16(pcm) -> bytes
#
# Packets are **raw PCM**, not Opus:
#  - float32 little-endian  (preferred)
#  - int16   little-endian  (fallback)
#
# Extra tolerance: if payload length isn't aligned to 4 or 2,
# we will trim up to 3 trailing bytes to recover a valid frame.
# (Useful if a tiny control trailer or stray byte slipped in.)
#
# Legacy attributes exposed:
#  - samples_per_frame (from rate & frame_ms)
#  - bytes_per_sample_float32, bytes_per_sample_int16
#
# Downmixes multi-channel -> mono if needed.

import numpy as np

class OpusDecoder:
    def __init__(self, channels=1, rate=16000, frame_ms=20, prefer_float32_packets=True):
        self.channels = int(channels) if channels else 1
        self.rate = int(rate) if rate else 16000
        self.frame_ms = int(frame_ms) if frame_ms else 20
        self.prefer_float32_packets = bool(prefer_float32_packets)
        # Legacy attributes
        self.bytes_per_sample_float32 = 4
        self.bytes_per_sample_int16 = 2

    @property
    def samples_per_frame(self) -> int:
        return int(self.rate * self.frame_ms / 1000)

    # -------------------- Decode --------------------
    def decode_to_float32(self, data: bytes) -> np.ndarray:
        if not data:
            return np.zeros(0, dtype=np.float32)

        n = len(data)

        # Try direct float32 decode
        if n % 4 == 0:
            try:
                pcm = np.frombuffer(data, dtype='<f4')
                if self.channels > 1 and pcm.size % self.channels == 0:
                    pcm = pcm.reshape(-1, self.channels).mean(axis=1)
                pcm = np.clip(pcm, -1.0, 1.0).astype(np.float32, copy=False)
                return pcm
            except Exception:
                pass

        # Try direct int16 decode
        if n % 2 == 0:
            pcm_i16 = np.frombuffer(data, dtype='<i2')
            if self.channels > 1 and pcm_i16.size % self.channels == 0:
                pcm_i16 = pcm_i16.reshape(-1, self.channels).mean(axis=1).astype(np.int16, copy=False)
            pcm = (pcm_i16.astype(np.float32) / 32767.0)
            return pcm

        # Tolerant trimming: drop up to 3 trailing bytes to align
        for drop in (1, 2, 3):
            m = n - drop
            if m <= 0:
                break
            # Prefer float32 alignment first
            if m % 4 == 0:
                trimmed = data[:m]
                try:
                    pcm = np.frombuffer(trimmed, dtype='<f4')
                    if self.channels > 1 and pcm.size % self.channels == 0:
                        pcm = pcm.reshape(-1, self.channels).mean(axis=1)
                    pcm = np.clip(pcm, -1.0, 1.0).astype(np.float32, copy=False)
                    return pcm
                except Exception:
                    pass
            # Then try int16 alignment
            if m % 2 == 0:
                trimmed = data[:m]
                pcm_i16 = np.frombuffer(trimmed, dtype='<i2')
                if self.channels > 1 and pcm_i16.size % self.channels == 0:
                    pcm_i16 = pcm_i16.reshape(-1, self.channels).mean(axis=1).astype(np.int16, copy=False)
                pcm = (pcm_i16.astype(np.float32) / 32767.0)
                return pcm

        # If still invalid, give a clear error
        raise ValueError(f"buffer size must be a multiple of 4 or 2 (got {n} bytes)")

    # -------------------- Encode --------------------
    def encode_float32(self, pcm) -> bytes:
        """Encode mono float32 [-1,1] to float32-LE bytes (preferred)."""
        if pcm is None:
            return b''
        arr = np.asarray(pcm, dtype=np.float32)
        if arr.ndim > 1 and arr.shape[1] > 1:
            arr = arr.mean(axis=1).astype(np.float32, copy=False)
        np.clip(arr, -1.0, 1.0, out=arr)
        return arr.astype('<f4', copy=False).tobytes()

    def encode_int16(self, pcm) -> bytes:
        """Encode mono float32 [-1,1] (or int16) to int16-LE bytes."""
        if pcm is None:
            return b''
        arr = np.asarray(pcm)
        if arr.dtype != np.int16:
            if arr.ndim > 1 and arr.shape[1] > 1:
                arr = arr.mean(axis=1)
            arr = np.clip(arr.astype(np.float32), -1.0, 1.0)
            arr = (arr * 32767.0).astype('<i2', copy=False)
        else:
            arr = arr.astype('<i2', copy=False)
        return arr.tobytes()

    def encode(self, pcm) -> bytes:
        return self.encode_float32(pcm) if self.prefer_float32_packets else self.encode_int16(pcm)

# Back-compat alias
OpusShim = OpusDecoder
