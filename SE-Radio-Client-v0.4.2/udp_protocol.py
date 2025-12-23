# udp_protocol.py
from __future__ import annotations
import struct, time, threading
VER=1
MT_AUDIO,MT_CTRL,MT_ACK=0,1,2
CTRL_REGISTER,CTRL_HEARTBEAT,CTRL_PTT,CTRL_CHAN_UPD,CTRL_POSITION,CTRL_PRESENCE=1,2,3,4,5,6
# Admin/control-plane extra codes (admin_app -> server only)
CTRL_ADMIN_NET_MERGE=7
# Update workflow
# - Server -> Client: CTRL_UPDATE_OFFER with JSON payload describing the update.
# - Client -> Server: CTRL_UPDATE_RESPONSE with {"accept": true/false, "reason": "..."}
CTRL_UPDATE_OFFER=8
CTRL_UPDATE_RESPONSE=9
# HTTP port used by the server's built-in update file host/uploader.
UPDATE_HTTP_PORT=9876
AUDIO_FLAG_PTT=1
# Bit 1 signals that the payload is raw PCM (float32/int16). If clear, receivers may assume Opus.
AUDIO_FLAG_CODEC_PCM=2
# Bit 2 (client-only hint) marks PCM as int16; if clear, PCM is float32.
AUDIO_FLAG_PCM_I16=4
HDR_FMT="!BBHII";HDR_SZ=struct.calcsize(HDR_FMT)
AUDIO_HDR_FMT="!BH";AUDIO_HDR_SZ=struct.calcsize(AUDIO_HDR_FMT)
CTRL_HDR_FMT="!BH";CTRL_HDR_SZ=struct.calcsize(CTRL_HDR_FMT)
def pack_hdr(v,m,s,t,x):return struct.pack(HDR_FMT,v,m&0xFF,s&0xFFFF,t&0xFFFFFFFF,x&0xFFFFFFFF)
def unpack_hdr(b):return struct.unpack(HDR_FMT,b[:HDR_SZ])
def now_ts48():
 import time
 if not hasattr(now_ts48,"_t0"):now_ts48._t0=time.monotonic()
 return int((time.monotonic()-now_ts48._t0)*48000)
class SeqGen:
 def __init__(s):s._seq=0;s._lock=threading.Lock()
 def next(s): 
  with s._lock:s._seq=(s._seq+1)&0xFFFF;return s._seq
