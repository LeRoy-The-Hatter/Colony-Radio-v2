# opus_shim.py
import os, sys, shutil
try:import numpy as np
except:np=None

# Try to point opuslib at a bundled DLL if present (opus.dll or libopus-0.dll),
# covering both source checkout and PyInstaller (sys._MEIPASS) paths.
_dll_hint = None
def _candidate_dirs():
 try:
  base = os.path.dirname(__file__)
  yield base
 except Exception:
  pass
 try:
  yield os.getcwd()
 except Exception:
  pass
 try:
  mp = getattr(sys, "_MEIPASS", None)
  if mp:
   yield mp
 except Exception:
  pass

try:
 for _dir in _candidate_dirs():
  if not _dir:
   continue
  for _name in ("opus.dll", "libopus-0.dll"):
   _cand = os.path.join(_dir, _name)
   if os.path.isfile(_cand):
    _dll_hint = _cand
    # If we only found libopus-0.dll, attempt to provide an opus.dll alias so ctypes.util.find_library('opus') succeeds.
    try:
     if _name != "opus.dll":
      alias = os.path.join(_dir, "opus.dll")
      if not os.path.isfile(alias):
       shutil.copyfile(_cand, alias)
    except Exception:
     pass
    try:
     if hasattr(os, "add_dll_directory"):
      os.add_dll_directory(_dir)
     os.environ["PATH"] = _dir + os.pathsep + os.environ.get("PATH", "")
    except Exception:
     pass
    break
  if _dll_hint:
   break
 if _dll_hint and not os.environ.get("OPUSLIB_API_DLL"):
  os.environ["OPUSLIB_API_DLL"] = _dll_hint
except Exception:
 _dll_hint = None

OPUS_ERR=None
try:
 import opuslib
 from opuslib import Encoder,Decoder
 OPUS_OK=True
except Exception as e:
 OPUS_OK=False; OPUS_ERR=e
 # Try to pull opuslib from a bundled venv/site-packages if running with system Python.
 try:
  import sys as _sys, os as _os
  from pathlib import Path as _Path
  _base = _Path(__file__).resolve().parent
  _extras = [
   _base / "venv" / "Lib" / "site-packages",
   _base / "Lib" / "site-packages",
  ]
  added=False
  for _p in _extras:
   try:
    if _p.exists():
     _sys.path.insert(0, str(_p))
     added=True
   except Exception:
    pass
  if added:
   try:
    import opuslib  # type: ignore
    from opuslib import Encoder, Decoder  # type: ignore
    OPUS_OK=True
    OPUS_ERR=None
   except Exception as e2:
    OPUS_OK=False; OPUS_ERR=e2
 except Exception:
  pass
class OpusShim:
 def __init__(s,rate=48000,channels=1,frame_ms=10):
  s.dll=_dll_hint
  s.err=OPUS_ERR
  s.enabled=bool(OPUS_OK and np is not None);s.rate=rate;s.channels=channels;s.frame_ms=frame_ms
  s.samples_per_frame=int(rate*frame_ms/1000)
  if not s.enabled:return
  s.enc=Encoder(rate,channels,opuslib.APPLICATION_VOIP);s.dec=Decoder(rate,channels)
  s._decoders={}
  try:
    # Quality tuning without raising target bitrate further.
    # Keep VBR on (unconstrained) so speech segments can borrow bits when needed,
    # disable FEC and loss padding to avoid added artifacts when the link is clean.
    s.enc.bitrate=128000
    s.enc.complexity=10
    s.enc.vbr=1
    s.enc.vbr_constraint=0
    s.enc.set_inband_fec(0)
    s.enc.set_packet_loss_perc(0)
    s.enc.set_dtx(0)
    s.enc.signal=opuslib.SIGNAL_VOICE
  except Exception:
   pass
 def _get_decoder(s,key=None):
  if not s.enabled:return None
  if key is None:return s.dec
  dec=s._decoders.get(key)
  if dec is None:
   try:
    dec=Decoder(s.rate,s.channels);s._decoders[key]=dec
    # Keep cache bounded so stale SSRCs do not leak decoders.
    if len(s._decoders)>32:
     try:
      oldest=next(iter(s._decoders))
      if oldest!=key:
       s._decoders.pop(oldest,None)
     except Exception:
      pass
   except Exception:
    dec=None
  return dec
 def encode_float32(s,a):
  if not s.enabled:return b""
  import numpy as np
  if len(a)<s.samples_per_frame:a=np.pad(a,(0,s.samples_per_frame-len(a)))
  a=np.clip(a,-1,1);pcm16=(a*32767).astype('<i2');return s.enc.encode(pcm16.tobytes(),s.samples_per_frame)
 def decode_to_float32(s,b,ssrc=None):
  if not s.enabled:return None
  dec=s._get_decoder(ssrc)
  if dec is None:return None
  pcm16=dec.decode(b,s.samples_per_frame,False)
  import numpy as np;return np.frombuffer(pcm16,dtype='<i2').astype(np.float32)/32767.0
