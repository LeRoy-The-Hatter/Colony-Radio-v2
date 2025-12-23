from __future__ import annotations

from collections import defaultdict
from time import time
from typing import Dict, Tuple, Optional, List
import random
import string


class Session:
    def __init__(
        self,
        addr: Tuple[str, int],
        ssrc: int,
        client_id: Optional[str] = None,
        nick: str = "",
        net: str = "A",
    ) -> None:
        self.addr = addr
        self.ssrc = int(ssrc)
        # Do NOT synthesize "ssrc:<num>" here; keep whatever the client sends.
        self.client_id = client_id
        self.nick = nick or ""
        # Legacy field; no longer used for routing, but kept for compatibility.
        self.net = net or ""

        # Channel / scan / PTT state
        self.active_channel: int = 0
        self.freqs: List[float] = [0.0, 0.0, 0.0, 0.0]
        self.scan: bool = False
        # Per-channel scan flags (A–D). This is kept in addition to the global scan bool.
        self.scan_channels: List[bool] = [False, False, False, False]

        self.current_tx_freq: Optional[float] = None
        self.ptt: bool = False
        self.loopback: bool = False

        # Position (for future SE bridge)
        self.position: Optional[Dict] = None
        self.last_seen: float = time()

        # Per-channel network prefixes: 3-letter codes owned by this client.
        # These stay fixed for the session; the numeric part comes from the frequency.
        self.net_prefixes: List[str] = [self._make_prefix() for _ in range(4)]

    # ---------------- network helpers ----------------

    @staticmethod
    def _make_prefix() -> str:
        # Random 3-letter uppercase prefix
        letters = string.ascii_uppercase
        return "".join(random.choice(letters) for _ in range(3))

    @staticmethod
    def _freq_suffix(freq: float) -> str:
        """Convert a frequency in MHz to the 4-digit numeric part.

        Rule (per user spec):
        - Multiply by 10
        - Truncate to int (no rounding)
        - Clamp to [0, 9999]
        - Format as 4 digits with leading zeros
        """
        try:
            f = float(freq)
        except (TypeError, ValueError):
            f = 0.0
        n = int(f * 10.0)
        if n < 0:
            n = 0
        if n > 9999:
            n = 9999
        return f"{n:04d}"

    def compute_networks(self):
        """Return (net_ids, active_net, summary_str) for this session.

        net_ids:   ['AAA0000', 'BBB1234', 'CCC0450', 'DDD0890']
        active_net: the ID for active_channel
        summary:   'A:AAA0000  B:*BBB1234  C:CCC0450  D:DDD0890'
        """
        labels = ["A", "B", "C", "D"]
        net_ids: List[str] = []

        # Ensure we have 4 prefixes
        if len(self.net_prefixes) < 4:
            self.net_prefixes = (self.net_prefixes + [self._make_prefix()] * 4)[:4]
        elif len(self.net_prefixes) > 4:
            self.net_prefixes = self.net_prefixes[:4]

        # Build per-channel net IDs
        for i in range(4):
            prefix = self.net_prefixes[i]
            freq = self.freqs[i] if i < len(self.freqs) else 0.0
            suffix = self._freq_suffix(freq)
            net_ids.append(prefix + suffix)

        try:
            active_idx = int(self.active_channel)
        except Exception:
            active_idx = 0
        active_idx = max(0, min(3, active_idx))
        active_net = net_ids[active_idx] if net_ids else ""

        parts = []
        for i, label in enumerate(labels):
            nid = net_ids[i] if i < len(net_ids) else ""
            mark = "*" if i == active_idx else ""
            parts.append(f"{label}:{mark}{nid}")
        summary = "  ".join(parts)

        return net_ids, active_net, summary

    def to_row(self) -> Dict:
        # Snapshot for admin UI
        net_ids, active_net, net_summary = self.compute_networks()
        return {
            "client_id": self.client_id,
            "nick": self.nick,
            # "net" now contains the human-readable summary of all networks for this client.
            "net": net_summary,
            "net_ids": net_ids,
            "active_net": active_net,
            "ssrc": self.ssrc,
            "addr": f"{self.addr[0]}:{self.addr[1]}",
            "active_channel": self.active_channel,
            "freqs": self.freqs,
            "scan": self.scan,
            "scan_channels": self.scan_channels,
            "tx_freq": self.current_tx_freq,
            "ptt": self.ptt,
            "last_seen": self.last_seen,
            # Game/bridge metadata (e.g., SE position)
            "position": self.position,
        }


class SessionManager:
    def __init__(self) -> None:
        self.by_addr: Dict[Tuple[str, int], Session] = {}
        self.by_ssrc: Dict[int, Session] = {}
        self.freq_frames = defaultdict(int)
        self.freq_bytes = defaultdict(int)

        # Server-side network aliases: original_nid -> canonical_nid
        self.net_alias: Dict[str, str] = {}
        # When enabled, networks with the same channel frequency collapse together.
        self.auto_merge_by_freq: bool = False

    # ---------------- alias helpers ----------------

    @staticmethod
    def _freq_suffix_from_net(nid: str) -> Optional[str]:
        nid = (nid or "").strip()
        if len(nid) < 4:
            return None
        suffix = nid[-4:]
        if not suffix.isdigit():
            return None
        try:
            val = int(suffix)
        except Exception:
            return None
        if val <= 0:
            return None
        return f"{val:04d}"

    @staticmethod
    def _auto_canon_for_suffix(suffix: str) -> str:
        try:
            mhz = int(suffix) / 10.0
            freq_str = f"{mhz:.1f}".rstrip("0").rstrip(".")
        except Exception:
            freq_str = suffix
        return f"FREQ-{freq_str}"

    def canonical_net(self, nid: str) -> str:
        nid = (nid or "").strip()
        if not nid:
            return ""
        seen = set()
        cur = nid
        had_manual_alias = False
        while cur in self.net_alias and cur not in seen:
            had_manual_alias = True
            seen.add(cur)
            cur = self.net_alias[cur]

        if self.auto_merge_by_freq and not had_manual_alias:
            suffix = self._freq_suffix_from_net(cur)
            if suffix:
                cur = self._auto_canon_for_suffix(suffix)

        return cur

    def merge_net(self, src: str, dst: str) -> None:
        """Record that 'src' network ID should be treated as 'dst'.

        This is used by the admin_app to force two networks to be
        considered equivalent for routing and presence snapshots.
        """
        src = (src or "").strip()
        dst = (dst or "").strip()
        if not src or not dst or src == dst:
            return
        dst_canon = self.canonical_net(dst)
        self.net_alias[src] = dst_canon

    def reset_net_aliases(self) -> None:
        """Clear all manual network aliases (auto-merge stays unchanged)."""
        self.net_alias.clear()

    def set_auto_merge_by_freq(self, enabled: bool) -> None:
        """Toggle automatic merging of networks that share the same frequency."""
        self.auto_merge_by_freq = bool(enabled)

    # ---------------- basic session tracking ----------------

    def upsert(self, addr: Tuple[str, int], ssrc: int, **meta) -> Session:
        # Create or update a session for this (addr, ssrc).
        key = int(ssrc)
        s = self.by_ssrc.get(key)

        client_id = meta.get("client_id")
        # If we don't yet have a session for this SSRC but *do* have a session
        # with the same client_id (Steam ID), reuse that session and rebind its SSRC.
        if s is None and client_id:
            for old_key, existing in list(self.by_ssrc.items()):
                if existing.client_id and existing.client_id == client_id:
                    s = existing
                    if old_key != key:
                        del self.by_ssrc[old_key]
                        self.by_ssrc[key] = s
                    break

        if s is None:
            s = Session(
                addr,
                ssrc,
                client_id=client_id,
                nick=meta.get("nick", ""),
                net=meta.get("net", ""),
            )
            self.by_ssrc[key] = s
            self.by_addr[addr] = s
        else:
            # Existing session: update address and metadata.
            s.addr = addr
            if "client_id" in meta and meta["client_id"]:
                s.client_id = meta["client_id"]
            if "nick" in meta and meta["nick"]:
                s.nick = meta["nick"]
            if "net" in meta and meta["net"]:
                # Legacy; kept only so note_presence can still update it if needed.
                s.net = meta["net"]
        if "loopback" in meta:
            try:
                s.loopback = bool(meta.get("loopback"))
            except Exception:
                pass
        s.last_seen = time()
        return s

    def note_heartbeat(self, ssrc: int) -> None:
        s = self.by_ssrc.get(int(ssrc))
        if not s:
            return
        s.last_seen = time()

    def drop(self, ssrc: int) -> None:
        """Remove a session from tracking (used when client declines update)."""
        try:
            key = int(ssrc)
        except Exception:
            return
        s = self.by_ssrc.pop(key, None)
        if s and getattr(s, "addr", None) in self.by_addr:
            try:
                self.by_addr.pop(s.addr, None)
            except Exception:
                pass

    # ---------------- channel / PTT state ----------------

    def set_channel(self, ssrc: int, active_idx: int, freqs, scan: bool) -> None:
        s = self.by_ssrc.get(int(ssrc))
        if not s:
            return
        try:
            s.active_channel = int(active_idx)
        except Exception:
            pass
        try:
            if isinstance(freqs, (list, tuple)) and len(freqs) == 4:
                s.freqs = [float(x) for x in freqs]
        except Exception:
            pass
        if isinstance(scan, bool):
            s.scan = scan
        s.last_seen = time()

    def set_tx_state(self, ssrc: int, on: bool, freq: Optional[float] = None) -> None:
        s = self.by_ssrc.get(int(ssrc))
        if not s:
            return
        s.ptt = bool(on)
        if on:
            if freq is not None:
                try:
                    s.current_tx_freq = float(freq)
                except Exception:
                    s.current_tx_freq = None
            else:
                idx = max(0, min(3, int(s.active_channel)))
                try:
                    s.current_tx_freq = float(s.freqs[idx])
                except Exception:
                    s.current_tx_freq = None
        else:
            s.current_tx_freq = None
        s.last_seen = time()

    def note_ptt(self, ssrc: int, on: bool) -> None:
        # Convenience hook for CTRL_PTT; we don't get a frequency here,
        # so we rely on whatever set_channel() last configured.
        self.set_tx_state(ssrc, on, None)

    def note_chan_update(self, ssrc: int, upd: Dict) -> None:
        # Public API called by server on CTRL_CHAN_UPD.
        if not isinstance(upd, dict):
            return
        active = upd.get("active", 0)
        freqs = upd.get("freqs", [])
        scan = bool(upd.get("scan", False))
        # Optional per-channel scan flags.
        scan_channels = upd.get("scan_channels")

        # Update basic channel state.
        self.set_channel(ssrc, active, freqs, scan)

        # If we have per-channel scan info, normalize and store it.
        sc = None
        if isinstance(scan_channels, (list, tuple)):
            try:
                sc = [bool(x) for x in scan_channels]
            except Exception:
                sc = None
        if sc is not None:
            if len(sc) < 4:
                sc = (sc + [False, False, False, False])[:4]
            else:
                sc = sc[:4]
            s = self.by_ssrc.get(int(ssrc))
            if s is not None:
                s.scan_channels = sc
                s.last_seen = time()

    # ---------------- presence / metadata ----------------

    def note_position(self, ssrc: int, pos: Dict) -> None:
        s = self.by_ssrc.get(int(ssrc))
        if not s:
            return
        if isinstance(pos, dict):
            s.position = pos
        s.last_seen = time()

    def note_presence(self, ssrc: int, info: Dict) -> None:
        # Optional presence metadata hook for CTRL_PRESENCE with a body.
        if not isinstance(info, dict):
            return
        s = self.by_ssrc.get(int(ssrc))
        if not s:
            return
        if info.get("nick"):
            s.nick = info["nick"]
        if info.get("net"):
            # Legacy; kept in case older clients still send 'net'.
            s.net = info["net"]
        if info.get("client_id"):
            s.client_id = info["client_id"]
        if "loopback" in info:
            try:
                s.loopback = bool(info.get("loopback"))
            except Exception:
                pass
        s.last_seen = time()

    # ---------------- audio stats ----------------

    def note_audio_for(self, ssrc: int, nbytes: int) -> None:
        s = self.by_ssrc.get(int(ssrc))
        key = (
            f"{float(s.current_tx_freq):.3f}"
            if (s and s.current_tx_freq is not None)
            else "unknown"
        )
        self.freq_frames[key] += 1
        self.freq_bytes[key] += int(nbytes)
        if s:
            s.last_seen = time()

    # ---------------- snapshots for admin UI ----------------

    def audio_recipients_for(self, ssrc: int):
        """Return (recipients, sender_active_net, chan_idx, chan_net) for a sender.

        recipients: list of (Session, recv_chan_idx) pairs that should receive the audio.
                    recv_chan_idx is the receiver's channel index (0-3) that matched.
        sender_active_net: canonical net ID for sender's active channel (for logging).
        chan_idx: sender's active channel index (0-3).
        chan_net: canonical net ID for the sender's active channel (preferred for routing).

        A recipient is included if:
        - It is not the sender.
        - It is within ACTIVE_TIMEOUT.
        - It shares the same canonical net ID on *any* of its channels AND
          either (a) that matching channel is the receiver's active channel,
          or (b) that matching channel has scan_channels[...] enabled.
        """
        try:
            key = int(ssrc)
        except Exception:
            return [], "", 0, ""

        sender = self.by_ssrc.get(key)
        if sender is None:
            return [], "", 0, ""

        try:
            sender_net_ids, sender_active_net, _ = sender.compute_networks()
        except Exception:
            sender_net_ids, sender_active_net = [], ""

        try:
            chan_idx = max(0, min(3, int(getattr(sender, "active_channel", 0))))
        except Exception:
            chan_idx = 0

        sender_active_net = self.canonical_net(sender_active_net)
        sender_chan_net = ""
        if sender_net_ids and len(sender_net_ids) > chan_idx:
            sender_chan_net = self.canonical_net(sender_net_ids[chan_idx])

        if not sender_chan_net:
            return [], sender_active_net, chan_idx, sender_chan_net

        now = time()
        ACTIVE_TIMEOUT = 15.0  # seconds, keep in sync with presence_snapshot
        recipients: List[Tuple[Session, int]] = []
        seen = set()

        for other in self.by_ssrc.values():
            if other is sender:
                continue
            try:
                last = float(getattr(other, "last_seen", 0.0))
            except Exception:
                last = 0.0
            if now - last > ACTIVE_TIMEOUT:
                continue

            # Compute other nets and canonical IDs
            try:
                other_net_ids, _, _ = other.compute_networks()
            except Exception:
                other_net_ids = []

            if not other_net_ids:
                continue

            # Canonicalize all of the receiver's nets so we can match across channels.
            canon_nets = [self.canonical_net(n) for n in other_net_ids]
            match_idxs = [i for i, nid in enumerate(canon_nets) if nid == sender_chan_net]
            if not match_idxs:
                continue

            # Only deliver if a matching channel is active or explicitly scanned.
            deliver_idx = None
            for idx in match_idxs:
                chan_scan_flag = False
                try:
                    sc = getattr(other, "scan_channels", None)
                    if sc is not None and len(sc) > idx:
                        chan_scan_flag = bool(sc[idx])
                except Exception:
                    chan_scan_flag = False

                try:
                    active_match = int(getattr(other, "active_channel", 0)) == idx
                except Exception:
                    active_match = False

                if active_match:
                    deliver_idx = idx
                    break  # prefer the active channel if multiple match
                if chan_scan_flag and deliver_idx is None:
                    deliver_idx = idx  # fall back to the first scanned match

            if deliver_idx is not None and id(other) not in seen:
                recipients.append((other, deliver_idx))
                seen.add(id(other))

        # Optional server-side loopback for debug: route sender back to itself.
        try:
            if getattr(sender, "loopback", False):
                recipients.append((sender, chan_idx))
        except Exception:
            pass

        return recipients, sender_active_net, chan_idx, sender_chan_net

    def presence_snapshot(self) -> List[Dict]:
        # Return only active sessions for admin presence snapshots.
        # Hide the admin poller itself (ssrc==0).
        now = time()
        ACTIVE_TIMEOUT = 15.0  # seconds
        player_ids = set()

        # Collect active Steam IDs reported by the game bridge so we can flag linked clients.
        for s in self.by_ssrc.values():
            try:
                last = float(getattr(s, "last_seen", 0.0))
            except Exception:
                last = 0.0
            if now - last > ACTIVE_TIMEOUT:
                continue
            pos = getattr(s, "position", None)
            if not isinstance(pos, dict):
                continue
            if pos.get("type") == "antenna_snapshot":
                continue
            for key in ("steam_id", "guid"):
                val = pos.get(key)
                if val in (None, ""):
                    continue
                try:
                    sval = str(val)
                except Exception:
                    continue
                if sval:
                    player_ids.add(sval)

        rows: List[Dict] = []
        for s in self.by_ssrc.values():
            if int(getattr(s, "ssrc", 0)) == 0:
                continue
            if now - float(getattr(s, "last_seen", 0.0)) <= ACTIVE_TIMEOUT:
                row = s.to_row()
                # Canonicalize network IDs for the admin view.
                net_ids = row.get("net_ids") or []
                if isinstance(net_ids, (list, tuple)) and len(net_ids) == 4:
                    canon_ids = [self.canonical_net(n) for n in net_ids]
                else:
                    canon_ids = ["", "", "", ""]

                active_net = self.canonical_net(row.get("active_net", ""))

                # Rebuild summary string using canonical IDs.
                try:
                    active_idx = int(row.get("active_channel", 0))
                except Exception:
                    active_idx = 0
                active_idx = max(0, min(3, active_idx))
                labels = ["A", "B", "C", "D"]
                parts = []
                for i, label in enumerate(labels):
                    nid = canon_ids[i] if i < len(canon_ids) else ""
                    mark = "*" if i == active_idx else ""
                    parts.append(f"{label}:{mark}{nid}")
                summary = "  ".join(parts)

                row["net_ids"] = canon_ids
                row["active_net"] = active_net
                row["net"] = summary
                try:
                    cid_val = row.get("client_id")
                    cid_str = str(cid_val)
                except Exception:
                    cid_str = ""
                    cid_val = None
                row["linked_player"] = bool(cid_val not in (None, "") and cid_str in player_ids)
                rows.append(row)
        return rows

    def summarize_frequencies(self, top_n: int = 6):
        pairs = []
        for k, frames in self.freq_frames.items():
            b = self.freq_bytes.get(k, 0)
            pairs.append((k, frames, b / 1024.0))
        pairs.sort(key=lambda t: t[1], reverse=True)
        return pairs[:top_n]
