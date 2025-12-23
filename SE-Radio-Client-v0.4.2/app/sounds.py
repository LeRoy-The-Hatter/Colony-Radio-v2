import os
import sys
from pathlib import Path
try:
    import pygame
    _HAVE_PYGAME = True
except Exception:
    _HAVE_PYGAME = False

def _package_root() -> Path:
    """
    Resolve the install root whether running from source or PyInstaller.
    For the exe, PyInstaller exposes a temp dir via sys._MEIPASS.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent

class SoundPlayer:
    def __init__(self, base_dir=None):
        # Default to project root (handles PyInstaller _MEIPASS) so Audio/ is found when frozen.
        self.base_dir = str(base_dir or _package_root())
        self.initialized = False
        self.keyup = None
        self.unkey = None
        self.switch = None  # channel switch sound
        self.vol = None     # NEW: volume change sound
        self.master_gain = 1.0  # 1.0 = normal, 0.0 = mute, 2.0 = boosted
        self._boost_cache = {}

    def _path(self, name):
        return os.path.join(self.base_dir, "Audio", name)

    def ensure_init(self):
        if not _HAVE_PYGAME or self.initialized:
            return
        try:
            # Initialize mixer lazily with safe defaults
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.init()
            pygame.mixer.init()
            # Load existing sounds (optional)
            try:
                kp = self._path("keyup.mp3")
                if os.path.exists(kp):
                    self.keyup = pygame.mixer.Sound(kp)
            except Exception:
                self.keyup = None
            try:
                uk = self._path("unkey.mp3")
                if os.path.exists(uk):
                    self.unkey = pygame.mixer.Sound(uk)
            except Exception:
                self.unkey = None
            # Load channel switch sound (optional)
            try:
                sw = self._path("buttonpress.mp3")
                if os.path.exists(sw):
                    self.switch = pygame.mixer.Sound(sw)
            except Exception:
                self.switch = None
            # NEW: Load volume change sound (optional)
            try:
                vp = self._path("volume.mp3")
                if os.path.exists(vp):
                    self.vol = pygame.mixer.Sound(vp)
            except Exception:
                self.vol = None
            self.initialized = True
            self._set_loaded_volume()
        except Exception:
            # If pygame init fails for any reason, keep it graceful
            self.initialized = False
            self.keyup = None
            self.unkey = None
            self.switch = None
            self.vol = None
            self._boost_cache.clear()

    def _set_loaded_volume(self):
        base = min(max(float(self.master_gain), 0.0), 1.0)
        for snd in (self.keyup, self.unkey, self.switch, self.vol):
            try:
                if snd:
                    snd.set_volume(base)
            except Exception:
                pass

    def set_gain(self, gain: float):
        """Set master SFX gain (0.0-2.0). Values >1.0 are handled via boosted copies."""
        try:
            g = float(gain)
        except Exception:
            g = 1.0
        g = max(0.0, min(2.0, g))
        if abs(g - self.master_gain) < 1e-6:
            return self.master_gain
        self.master_gain = g
        self._boost_cache.clear()
        if self.initialized:
            self._set_loaded_volume()
        return self.master_gain

    def _get_boosted_sound(self, sound, cache_key, gain):
        if not _HAVE_PYGAME or sound is None:
            return None
        try:
            import pygame, numpy as np
            g = max(1.0, min(2.0, float(gain)))
            key = (cache_key, round(g, 2))
            if key in self._boost_cache:
                return self._boost_cache[key]
            arr = pygame.sndarray.array(sound)
            arr_float = arr.astype("float32") * g
            if arr.dtype.kind in ("i", "u"):
                info = np.iinfo(arr.dtype)
                arr_scaled = np.clip(arr_float, info.min, info.max).astype(arr.dtype)
            else:
                arr_scaled = np.clip(arr_float, -1.0, 1.0).astype(arr.dtype)
            boosted = pygame.sndarray.make_sound(arr_scaled)
            self._boost_cache[key] = boosted
            return boosted
        except Exception:
            return None

    def _play(self, sound, cache_key=None):
        if not _HAVE_PYGAME or self.master_gain <= 0:
            return
        if not self.initialized:
            self.ensure_init()
        if sound is None:
            return
        if self.master_gain > 1.0:
            boosted = self._get_boosted_sound(sound, cache_key, self.master_gain)
            if boosted:
                try:
                    boosted.play()
                    return
                except Exception:
                    pass
        try:
            sound.set_volume(min(self.master_gain, 1.0))
        except Exception:
            pass
        try:
            sound.play()
        except Exception:
            pass

    def play_keyup(self):
        if not self.initialized:
            self.ensure_init()
        self._play(self.keyup, "keyup")

    def play_unkey(self):
        if not self.initialized:
            self.ensure_init()
        self._play(self.unkey, "unkey")

    def play_switch(self):
        """Play the 'channel switched' click/voice cue."""
        if not self.initialized:
            self.ensure_init()
        self._play(self.switch, "switch")

    def play_volume(self):
        """Play the 'volume changed' cue (volume.mp3) if present."""
        if not self.initialized:
            self.ensure_init()
        self._play(self.vol, "volume")
