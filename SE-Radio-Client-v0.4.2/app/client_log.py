
# client_log.py — tiny rotating logger for SE-Radio client
import os, sys, time, threading

class ClientLogger:
    def __init__(self, name="client", log_dir="logs", rotate_mb=5):
        self.name = name
        self.log_dir = log_dir
        self.rotate_bytes = int(rotate_mb * 1024 * 1024)
        self._lock = threading.Lock()
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            pass
        self._path = self._mk_path()
        self._fh = None
        self._open()

    def _mk_path(self):
        date = time.strftime("%Y-%m-%d")
        return os.path.join(self.log_dir, f"{self.name}-{date}.log")

    def _open(self):
        try:
            self._fh = open(self._path, "a", encoding="utf-8", buffering=1)
        except Exception:
            self._fh = None

    def _should_rotate(self):
        try:
            return os.path.getsize(self._path) >= self.rotate_bytes
        except Exception:
            return False

    def _rotate(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        ts = time.strftime("%H%M%S")
        try:
            os.rename(self._path, self._path.replace(".log", f".{ts}.log"))
        except Exception:
            pass
        self._open()

    def log(self, level, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{level}] {msg}\n"
        with self._lock:
            try:
                if self._fh:
                    if self._should_rotate():
                        self._rotate()
                    self._fh.write(line)
            except Exception:
                pass
        # also echo to console for now
        try:
            print(line.rstrip())
        except Exception:
            pass

    def info(self, msg): self.log("INFO", msg)
    def warn(self, msg): self.log("WARN", msg)
    def err(self, msg):  self.log("ERR ", msg)

# singleton helper
_default = None
def get_logger():
    global _default
    if _default is None:
        _default = ClientLogger()
    return _default
