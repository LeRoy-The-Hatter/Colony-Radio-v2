# test_opus_details.py
import opus_shim

print("np is None:", opus_shim.np is None)
print("OPUS_OK:", opus_shim.OPUS_OK)

from opus_shim import OpusShim
enc = OpusShim(rate=24000, channels=1, frame_ms=20)
print("enc.enabled:", enc.enabled)
