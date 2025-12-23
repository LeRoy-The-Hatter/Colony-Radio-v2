import numpy as np
import sounddevice as sd

class AudioEngine:
    """Simple single-channel IO engine using sounddevice.
    - start(): opens input/output streams (or defaults)
    - read_frame(): returns a mono float32 numpy array of length blocksize
    - write_frame(arr): writes mono float32 to output
    """
    def __init__(self, samplerate=48000, blocksize=480, input_device=None, output_device=None):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.input_device = input_device
        self.output_device = output_device
        self.instream = None
        self.outstream = None
        self._inbuf = np.zeros(self.blocksize, dtype=np.float32)

    def start(self):
        # Input
        self.instream = sd.InputStream(
            device=self.input_device,
            channels=1,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
        )
        # Output
        self.outstream = sd.OutputStream(
            device=self.output_device,
            channels=1,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
        )
        self.instream.start()
        self.outstream.start()

    def stop(self):
        try:
            if self.instream:
                self.instream.stop(); self.instream.close()
        finally:
            self.instream = None
        try:
            if self.outstream:
                self.outstream.stop(); self.outstream.close()
        finally:
            self.outstream = None

    def read_frame(self):
        try:
            data, _ = self.instream.read(self.blocksize)
            if data is None:
                return None
            # ensure mono
            if data.ndim == 2 and data.shape[1] > 1:
                data = data.mean(axis=1)  # downmix to mono
            return np.asarray(data, dtype=np.float32).reshape(-1)
        except Exception:
            return None

    def write_frame(self, arr):
        if arr is None:
            return
        # ensure 2D with channels=1
        try:
            arr2 = np.asarray(arr, dtype=np.float32).reshape(-1, 1)
            self.outstream.write(arr2)
        except Exception:
            pass
