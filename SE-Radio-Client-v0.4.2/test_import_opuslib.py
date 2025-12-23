# test_import_opuslib.py
import traceback

print("Testing import of opuslib...")
try:
    import opuslib
    from opuslib import Encoder, Decoder
    print("IMPORT OK, opuslib version:", getattr(opuslib, "__version__", "unknown"))
except Exception as e:
    print("IMPORT FAILED:", repr(e))
    traceback.print_exc()
