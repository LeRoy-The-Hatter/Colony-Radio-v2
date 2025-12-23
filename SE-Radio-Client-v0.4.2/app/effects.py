import numpy as np

class EdgeEffects:
    """Applies 'edge of range' artifacts.
    sqi in [0..1]: 1.0 = clean; lower adds static & dropouts.
    """
    def __init__(self, noise_floor_db=-28.0, dropout_prob=0.08):
        self.noise_floor_db = noise_floor_db
        self.dropout_prob = dropout_prob
        self._rng = np.random.default_rng()

    def apply(self, frame: np.ndarray, sqi: float) -> np.ndarray:
        if frame is None:
            return None
        out = frame.astype(np.float32).copy()

        # Static scaling: as sqi drops, increase noise gain
        noise_amp = 10 ** (self.noise_floor_db / 20.0)
        noise = self._rng.normal(0.0, 1.0, size=out.shape).astype(np.float32) * noise_amp * (1.0 - sqi)
        out = out * (0.85 + 0.15 * sqi) + noise

        # Simulate random 'cuts' near the edge by zeroing small spans
        # Probability increases as sqi decreases.
        p = self.dropout_prob * (1.0 - sqi)
        if p > 0:
            # Choose up to 3 tiny dropouts per frame
            n_drops = self._rng.integers(low=0, high=3)
            N = out.shape[0]
            for _ in range(n_drops):
                if self._rng.random() < p:
                    w = int(self._rng.integers(low=20, high=min(200, max(21, N//8))))
                    start = int(self._rng.integers(low=0, high=max(1, N - w)))
                    out[start:start+w] *= 0.0

        # Light limiter to avoid clipping
        out = np.clip(out, -1.0, 1.0)
        return out
