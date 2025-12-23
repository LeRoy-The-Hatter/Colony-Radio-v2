# admin_app.py — Admin UI for SE-Radio UDP server with Networks tab and server-side net merge
import argparse
import json
import os
import random
import socket
import struct
import math
import threading
import time
import tkinter as tk
import urllib.request
import urllib.error
import string
from collections import deque
from tkinter import ttk, filedialog

from udp_protocol import (
    VER,
    MT_CTRL,
    CTRL_PRESENCE,
    CTRL_ADMIN_NET_MERGE,
    CTRL_ADMIN_NET_AUTOMERGE,
    CTRL_ADMIN_NET_UNMERGE_ALL,
    CTRL_UPDATE_OFFER,
    CTRL_UPDATE_RESPONSE,
    UPDATE_HTTP_PORT,
    HDR_SZ,
    pack_hdr,
    now_ts48,
    SeqGen,
)

APP_TITLE = "SE-Radio • Admin (UDP)"
POLL_MS = 700  # ms between presence polls
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "admin_settings.json")
LOG_MAX_LINES = 2500
SERVER_LOG_PATH = os.path.join(os.path.dirname(__file__), "server.log")


class AdminApp:
    def __init__(self, root, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.root = root
        self.host, self.port = host, int(port)
        self.seq = SeqGen()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.3)
        self.root.title(APP_TITLE)

        self._rows = []
        self._stop = threading.Event()
        self._network_index = {}
        self._auto_merge_enabled = False
        self._manual_merge_count = 0
        # Cache of stable 3-letter network IDs keyed by component membership.
        self._net_id_cache = {}
        # Map of in-game player identifiers to their proximity network header.
        self._player_net_header = {}
        # Track net aliases already pushed to the server so we avoid re-sending identical merges.
        self._last_sent_net_alias = {}
        # Server log tail state
        self._server_log_lines = deque(maxlen=LOG_MAX_LINES)
        self._server_log_pending = deque(maxlen=LOG_MAX_LINES)
        self._server_log_partial = ""
        self._server_log_lock = threading.Lock()
        self._server_log_flush_scheduled = False
        self._server_log_offset = 0
        self._server_log_thread = None

        self._build_ui()
        self._load_settings()

        # Background RX thread
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

        # Start polling loop
        self._poll()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text=f"Server: {self.host}:{self.port}").pack(side="left")

        self.status = tk.StringVar(value="Polling…")
        ttk.Label(top, textvariable=self.status).pack(side="right")

        # Main notebook: Clients + Networks
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.clients_frame = ttk.Frame(self.notebook)
        self.networks_frame = ttk.Frame(self.notebook)
        self.game_frame = ttk.Frame(self.notebook)
        self.update_frame = ttk.Frame(self.notebook)
        self.log_frame = ttk.Frame(self.notebook)

        self.notebook.add(self.clients_frame, text="Clients")
        self.notebook.add(self.networks_frame, text="Networks")
        self.notebook.add(self.game_frame, text="Game")
        self.notebook.add(self.update_frame, text="Update")
        self.notebook.add(self.log_frame, text="Log")

        self._build_clients_tab()
        self._build_networks_tab()
        self._build_game_tab()
        self._build_update_tab()
        self._build_log_tab()

    def _build_clients_tab(self) -> None:
        cols = (
            "client_id",
            "nick",
            "net",
            "ssrc",
            "ptt",
            "chan_a",
            "chan_b",
            "chan_c",
            "chan_d",
            "scan",
            "active_chan",
            "addr",
            "last",
        )
        self.tree = ttk.Treeview(self.clients_frame, columns=cols, show="headings", height=18)
        for key, header in [
            ("client_id", "Client ID (Steam)"),
            ("nick", "Player Linked"),
            ("net", "Networks"),
            ("ssrc", "SSRC"),
            ("ptt", "PTT"),
            ("chan_a", "Chan A (MHz)"),
            ("chan_b", "Chan B (MHz)"),
            ("chan_c", "Chan C (MHz)"),
            ("chan_d", "Chan D (MHz)"),
            ("scan", "Scan"),
            ("active_chan", "Active"),
            ("addr", "Remote"),
            ("last", "Last Seen"),
        ]:
            self.tree.heading(key, text=header)
            if key in ("client_id", "addr"):
                width = 150
            elif key in ("chan_a", "chan_b", "chan_c", "chan_d"):
                width = 90
            elif key == "scan":
                width = 90
            elif key == "net":
                width = 260
            elif key == "nick":
                width = 110
            else:
                width = 80
            self.tree.column(key, anchor="w", width=width)

        self.tree.pack(fill="both", expand=True)

    def _build_networks_tab(self) -> None:
        # Upper: networks overview
        upper = ttk.Frame(self.networks_frame)
        upper.pack(fill="both", expand=True)

        net_cols = ("net_id", "members", "details")
        self.network_tree = ttk.Treeview(upper, columns=net_cols, show="headings", height=8)
        for key, header, width in [
            ("net_id", "Network ID", 160),
            ("members", "# Nodes", 90),
            ("details", "Members", 320),
        ]:
            self.network_tree.heading(key, text=header)
            self.network_tree.column(key, anchor="w", width=width)
        self.network_tree.pack(fill="both", expand=True, pady=(0, 8))

        self.network_tree.bind("<<TreeviewSelect>>", self._on_network_select)

        # Middle: members of selected network
        mid = ttk.Frame(self.networks_frame)
        mid.pack(fill="both", expand=True)

        member_cols = ("type", "name", "identifier", "server", "x", "y", "z", "range", "last")
        self.members_tree = ttk.Treeview(mid, columns=member_cols, show="headings", height=8)
        for key, header, width in [
            ("type", "Type", 80),
            ("name", "Name", 160),
            ("identifier", "Identifier", 160),
            ("server", "Server", 100),
            ("x", "X", 80),
            ("y", "Y", 80),
            ("z", "Z", 80),
            ("range", "Range", 90),
            ("last", "Last Seen", 90),
        ]:
            self.members_tree.heading(key, text=header)
            self.members_tree.column(key, anchor="w", width=width)
        self.members_tree.pack(fill="both", expand=True, pady=(0, 8))

        # Bottom: merge UI
        bottom = ttk.LabelFrame(self.networks_frame, text="Merge networks (server-side)")
        bottom.pack(fill="x", pady=(4, 0))

        self.net_from_var = tk.StringVar()
        self.net_to_var = tk.StringVar()
        self.merge_status = tk.StringVar(value="")
        self.auto_merge_var = tk.BooleanVar(value=False)
        self.manual_merge_count_var = tk.StringVar(value="Manual merges: 0")

        opts = ttk.Frame(bottom)
        opts.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Checkbutton(
            opts,
            text="Auto Merge Networks (same frequency)",
            variable=self.auto_merge_var,
            command=self._on_auto_merge_toggle,
        ).pack(side="left")
        self.freq_mode_btn = ttk.Button(
            opts,
            text="Enable Freq-Only Networks (ignore headers)",
            command=self._on_freq_mode_click,
        )
        self.freq_mode_btn.pack(side="left", padx=(12, 0))
        ttk.Button(opts, text="Un-Merge All", command=self._on_unmerge_all).pack(side="left", padx=(12, 0))
        ttk.Label(opts, textvariable=self.manual_merge_count_var).pack(side="left", padx=(12, 0))
        self._update_freq_mode_button()

        merge_row = ttk.Frame(bottom)
        merge_row.pack(fill="x", padx=8, pady=(0, 6))

        ttk.Label(merge_row, text="From:").pack(side="left", padx=(0, 4))
        self.net_from_cb = ttk.Combobox(merge_row, textvariable=self.net_from_var, width=18, state="readonly")
        self.net_from_cb.pack(side="left", padx=(0, 12))

        ttk.Label(merge_row, text="Into:").pack(side="left", padx=(0, 4))
        self.net_to_cb = ttk.Combobox(merge_row, textvariable=self.net_to_var, width=18, state="readonly")
        self.net_to_cb.pack(side="left", padx=(0, 12))

        ttk.Button(merge_row, text="Merge", command=self._on_merge_click).pack(side="left", padx=(0, 12))

        ttk.Label(merge_row, textvariable=self.merge_status).pack(side="left", padx=(0, 8))

    def _build_game_tab(self) -> None:
        wrapper = ttk.Frame(self.game_frame)
        wrapper.pack(fill="both", expand=True)

        # Players section
        players_box = ttk.LabelFrame(wrapper, text="Players")
        players_box.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        player_opts = ttk.Frame(players_box)
        player_opts.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(player_opts, text="Antenna Range (m):").pack(side="left")
        self.player_antenna_range_var = tk.StringVar()
        self.player_antenna_range_var.trace_add("write", self._on_range_change)
        ttk.Entry(player_opts, textvariable=self.player_antenna_range_var, width=12).pack(side="left", padx=(4, 0))
        ttk.Label(player_opts, text="Player Distortion (m):").pack(side="left", padx=(14, 0))
        self.player_distortion_range_var = tk.StringVar()
        self.player_distortion_range_var.trace_add("write", self._on_range_change)
        ttk.Entry(player_opts, textvariable=self.player_distortion_range_var, width=12).pack(side="left", padx=(4, 0))

        player_cols = ("server", "guid", "steam_id", "identity_id", "x", "y", "z", "distortion", "last")
        self.game_tree = ttk.Treeview(players_box, columns=player_cols, show="headings", height=10)
        player_headers = [
            ("server", "Server"),
            ("guid", "GUID"),
            ("steam_id", "Steam ID"),
            ("identity_id", "Identity ID"),
            ("x", "X"),
            ("y", "Y"),
            ("z", "Z"),
            ("distortion", "Distortion"),
            ("last", "Last Seen"),
        ]
        for key, header in player_headers:
            if key in ("guid", "steam_id"):
                width = 130
            elif key == "identity_id":
                width = 100
            elif key == "server":
                width = 100
            elif key == "distortion":
                width = 130
            else:
                width = 80
            self.game_tree.heading(key, text=header)
            self.game_tree.column(key, anchor="w", width=width)
        self.game_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # Antennas section
        antennas_box = ttk.LabelFrame(wrapper, text="Antenna Blocks (powered & functional)")
        antennas_box.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        antenna_opts = ttk.Frame(antennas_box)
        antenna_opts.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(antenna_opts, text="Antenna Range (m):").pack(side="left")
        self.antenna_range_var = tk.StringVar()
        self.antenna_range_var.trace_add("write", self._on_range_change)
        ttk.Entry(antenna_opts, textvariable=self.antenna_range_var, width=12).pack(side="left", padx=(4, 0))
        ttk.Label(antenna_opts, text="Antenna Distortion (m):").pack(side="left", padx=(14, 0))
        self.antenna_distortion_range_var = tk.StringVar()
        self.antenna_distortion_range_var.trace_add("write", self._on_range_change)
        ttk.Entry(antenna_opts, textvariable=self.antenna_distortion_range_var, width=12).pack(side="left", padx=(4, 0))

        antenna_cols = ("server", "name", "grid", "entity_id", "x", "y", "z", "distortion", "last")
        self.antenna_tree = ttk.Treeview(antennas_box, columns=antenna_cols, show="headings", height=8)
        antenna_headers = [
            ("server", "Server"),
            ("name", "Name"),
            ("grid", "Grid"),
            ("entity_id", "EntityId"),
            ("x", "X"),
            ("y", "Y"),
            ("z", "Z"),
            ("distortion", "Distortion"),
            ("last", "Last Seen"),
        ]
        for key, header in antenna_headers:
            if key in ("name", "grid"):
                width = 160
            elif key == "entity_id":
                width = 120
            elif key == "server":
                width = 100
            elif key == "distortion":
                width = 130
            else:
                width = 80
            self.antenna_tree.heading(key, text=header)
            self.antenna_tree.column(key, anchor="w", width=width)
        self.antenna_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # Save/apply controls
        save_row = ttk.Frame(wrapper)
        save_row.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Button(save_row, text="Save & Apply", command=self._on_save_game_settings).pack(side="left")
        self.game_save_status = tk.StringVar(value="")
        ttk.Label(save_row, textvariable=self.game_save_status, foreground="#555").pack(side="left", padx=(10, 0))

    def _build_update_tab(self) -> None:
        wrap = ttk.Frame(self.update_frame, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(
            wrap,
            text="Upload a new client .exe. Clients will be prompted to update the next time they connect.",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(0, 6))
        self.update_path_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.update_path_var, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse�?�", command=self._on_browse_update).pack(side="left", padx=(6, 0))

        ttk.Button(wrap, text="Update Client", command=self._on_update_click).pack(anchor="w", pady=(4, 6))

        self.update_status = tk.StringVar(value="Select an update file to begin.")
        ttk.Label(wrap, textvariable=self.update_status, foreground="#444").pack(anchor="w")

    def _build_log_tab(self) -> None:
        wrap = ttk.Frame(self.log_frame, padding=8)
        wrap.pack(fill="both", expand=True)

        server_box = ttk.LabelFrame(
            wrap, text=f"Server log (server.py → {os.path.basename(SERVER_LOG_PATH)})"
        )
        server_box.pack(fill="both", expand=True, pady=(0, 0))

        self.server_log_status = tk.StringVar(
            value="Waiting for server.log (start server.py)..."
        )
        ttk.Label(
            server_box,
            textvariable=self.server_log_status,
            foreground="#555",
            justify="left",
            wraplength=620,
        ).pack(anchor="w", pady=(0, 4))

        server_text_frame = ttk.Frame(server_box)
        server_text_frame.pack(fill="both", expand=True)

        self.server_log_text = tk.Text(
            server_text_frame,
            wrap="none",
            undo=False,
            height=12,
            font=("Consolas", 9),
        )
        sy = ttk.Scrollbar(server_text_frame, orient="vertical", command=self.server_log_text.yview)
        sx = ttk.Scrollbar(server_text_frame, orient="horizontal", command=self.server_log_text.xview)
        self.server_log_text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.server_log_text.tag_configure("log", foreground="#1a1a1a")
        self.server_log_text.tag_configure("stderr", foreground="#b30000")
        self.server_log_text.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        server_text_frame.rowconfigure(0, weight=1)
        server_text_frame.columnconfigure(0, weight=1)

        for binding_target in (
            self.server_log_text,
        ):
            binding_target.bind("<Key>", self._on_log_key)
            binding_target.bind("<<Paste>>", lambda e: "break")
            binding_target.bind("<<Cut>>", lambda e: "break")
            binding_target.bind("<Control-v>", lambda e: "break")
            binding_target.bind("<Control-V>", lambda e: "break")
            binding_target.bind("<Button-2>", lambda e: "break")

        # Flush any buffered log lines into the UI now that it exists.
        self._schedule_server_log_flush()
        self._start_server_log_tail()

    def _on_log_key(self, event):
        """Block edits in the log viewer while allowing navigation and copy."""
        try:
            key = (event.keysym or "").lower()
            ctrl = bool(event.state & 0x4)
            if ctrl and key in ("c", "a"):
                return None
            if key in ("left", "right", "up", "down", "home", "end", "prior", "next"):
                return None
        except Exception:
            return "break"
        return "break"

    # ---------------- server log tail ----------------

    def _start_server_log_tail(self) -> None:
        if self._server_log_thread:
            return
        try:
            t = threading.Thread(target=self._server_log_tail_loop, daemon=True)
            self._server_log_thread = t
            t.start()
        except Exception:
            self._server_log_thread = None

    def _server_log_tail_loop(self) -> None:
        path = SERVER_LOG_PATH
        last_err = ""
        while not self._stop.is_set():
            try:
                if not os.path.isfile(path):
                    self._set_server_log_status(
                        f"Waiting for {os.path.basename(path)} (start server.py)..."
                    )
                    self._server_log_offset = 0
                    time.sleep(0.75)
                    continue
                size = os.path.getsize(path)
                if size < self._server_log_offset:
                    self._server_log_offset = 0
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._server_log_offset)
                    chunk = f.read()
                    self._server_log_offset = f.tell()
                if chunk:
                    self._handle_server_log_chunk(chunk)
                    self._set_server_log_status(f"Streaming {os.path.basename(path)}")
            except Exception as e:
                msg = f"Log read error: {e}"
                if msg != last_err:
                    self._set_server_log_status(msg)
                    last_err = msg
            time.sleep(0.35)

    def _handle_server_log_chunk(self, data) -> None:
        try:
            if isinstance(data, (bytes, bytearray)):
                text = bytes(data).decode("utf-8", "replace")
            else:
                text = str(data)
        except Exception:
            try:
                text = repr(data)
            except Exception:
                text = ""
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with self._server_log_lock:
            combined = self._server_log_partial + text
            parts = combined.split("\n")
            self._server_log_partial = parts.pop() if parts else ""
            for line in parts:
                entry = (line, "log")
                self._server_log_lines.append(entry)
                self._server_log_pending.append(entry)
        self._schedule_server_log_flush()

    def _schedule_server_log_flush(self) -> None:
        try:
            if self._server_log_flush_scheduled:
                return
            self._server_log_flush_scheduled = True
            self.root.after(50, self._flush_server_log_ui)
        except Exception:
            self._server_log_flush_scheduled = False

    def _flush_server_log_ui(self) -> None:
        self._server_log_flush_scheduled = False
        if not hasattr(self, "server_log_text"):
            return
        try:
            with self._server_log_lock:
                pending = list(self._server_log_pending)
                self._server_log_pending.clear()
        except Exception:
            pending = []
        if not pending:
            return
        try:
            for line, tag in pending:
                ttag = tag if tag in ("stderr", "log") else "log"
                self.server_log_text.insert("end", (line or "") + "\n", ttag)
            try:
                current_lines = int(self.server_log_text.index("end-1c").split(".")[0])
            except Exception:
                current_lines = 0
            excess = current_lines - LOG_MAX_LINES
            if excess > 0:
                self.server_log_text.delete("1.0", f"{excess + 1}.0")
            self.server_log_text.see("end")
        except Exception:
            pass

    def _set_server_log_status(self, msg: str) -> None:
        try:
            if hasattr(self, "server_log_status"):
                self.root.after(0, lambda m=msg: self.server_log_status.set(m))
        except Exception:
            pass


    def _update_freq_mode_button(self) -> None:
        """Refresh the freq-only toggle button label to reflect current state."""
        if not hasattr(self, "freq_mode_btn"):
            return
        try:
            enabled = bool(self.auto_merge_var.get())
        except Exception:
            enabled = False
        label = "Disable Freq-Only Networks" if enabled else "Enable Freq-Only Networks (ignore headers)"
        try:
            self.freq_mode_btn.config(text=label)
        except Exception:
            pass

    # ---------------- settings persistence ----------------

    def _load_settings(self) -> None:
        """Load saved admin UI settings (antenna ranges)."""
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        try:
            player_val = data.get("player_range", "")
            if isinstance(player_val, (int, float)):
                player_val = str(player_val)
            if isinstance(player_val, str):
                self.player_antenna_range_var.set(player_val)
        except Exception:
            pass
        try:
            antenna_val = data.get("antenna_range", "")
            if isinstance(antenna_val, (int, float)):
                antenna_val = str(antenna_val)
            if isinstance(antenna_val, str):
                self.antenna_range_var.set(antenna_val)
        except Exception:
            pass
        try:
            player_dist = data.get("player_distortion_range", data.get("distortion_range", ""))
            if isinstance(player_dist, (int, float)):
                player_dist = str(player_dist)
            if isinstance(player_dist, str):
                self.player_distortion_range_var.set(player_dist)
        except Exception:
            pass
        try:
            antenna_dist = data.get("antenna_distortion_range", data.get("distortion_range", ""))
            if isinstance(antenna_dist, (int, float)):
                antenna_dist = str(antenna_dist)
            if isinstance(antenna_dist, str):
                self.antenna_distortion_range_var.set(antenna_dist)
        except Exception:
            pass

    def _save_settings(self) -> None:
        """Persist admin UI settings (antenna ranges)."""
        payload = {
            "player_range": self.player_antenna_range_var.get() if hasattr(self, "player_antenna_range_var") else "",
            "antenna_range": self.antenna_range_var.get() if hasattr(self, "antenna_range_var") else "",
            "player_distortion_range": self.player_distortion_range_var.get() if hasattr(self, "player_distortion_range_var") else "",
            "antenna_distortion_range": self.antenna_distortion_range_var.get() if hasattr(self, "antenna_distortion_range_var") else "",
        }
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def _on_range_change(self, *_args) -> None:
        """Save ranges with a small debounce to avoid excessive writes."""
        try:
            if hasattr(self, "_save_settings_after"):
                self.root.after_cancel(self._save_settings_after)
        except Exception:
            pass
        try:
            self._save_settings_after = self.root.after(200, self._save_settings)
        except Exception:
            # Fallback: best effort immediate save
            self._save_settings()

    def _on_save_game_settings(self) -> None:
        """Manually save and immediately apply game tab settings."""
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self._render_rows()  # refresh with new ranges/distortion values
        except Exception:
            pass
        try:
            self.game_save_status.set("Saved & applied")
            self.root.after(1500, lambda: self.game_save_status.set(""))
        except Exception:
            pass

    # ---------------- helpers ----------------

    def _fmt_freq(self, v) -> str:
        try:
            f = float(v)
        except Exception:
            return ""
        if f <= 0.0:
            return ""
        s = f"{f:.3f}"
        s = s.rstrip("0").rstrip(".")
        return s

    def _fmt_coord(self, v) -> str:
        try:
            return f"{float(v):.1f}"
        except Exception:
            return ""

    def _parse_range(self, var: tk.StringVar) -> float:
        if var is None:
            return 0.0
        try:
            raw = var.get()
        except Exception:
            return 0.0
        try:
            val = float(raw)
        except Exception:
            return 0.0
        if val < 0.0:
            val = 0.0
        return val

    def _fmt_range(self, meters: float) -> str:
        try:
            m = float(meters)
        except Exception:
            return ""
        if m <= 0.0:
            return ""
        if m >= 1000.0:
            km = m / 1000.0
            return f"{km:.2f} km"
        return f"{m:.0f} m"

    def _compute_distortion_level(self, distance: float, range_m: float, distortion_window: float) -> float:
        """Return 0..1 distortion based on distance inside the distortion band."""
        try:
            d = float(distance)
            r = float(range_m)
            w = float(distortion_window)
        except Exception:
            return 0.0
        if r <= 0.0 or w <= 0.0:
            return 0.0
        w = min(r, max(0.0, w))
        start = r - w
        if d <= start:
            return 0.0
        if d >= r:
            return 1.0
        frac = (d - start) / w if w > 0 else 0.0
        return max(0.0, min(1.0, frac))

    def _fmt_distortion(self, own, net=None) -> str:
        def _pct(val):
            try:
                return f"{max(0.0, min(1.0, float(val))) * 100.0:.0f}%"
            except Exception:
                return ""

        own_s = _pct(own)
        net_s = _pct(net)
        if not own_s and not net_s:
            return ""
        if not net_s or own_s == net_s:
            return own_s
        if not own_s:
            return net_s
        return f"{own_s} (net {net_s})"

    def _freq_suffix(self, freq: float) -> str:
        try:
            f = float(freq)
        except Exception:
            f = 0.0
        n = int(f * 10.0)
        if n < 0:
            n = 0
        if n > 9999:
            n = 9999
        return f"{n:04d}"

    def _random_net_code(self, taken) -> str:
        letters = string.ascii_uppercase
        taken = set(taken or [])
        for _ in range(64):
            cand = "".join(random.choice(letters) for _ in range(3))
            if cand not in taken:
                return cand
        # Fallback: deterministic but still 3 letters.
        return "NET"

    def _resolve_player_net_header(self, row: dict):
        """Return the proximity-derived network header (3 letters) for a row, if any."""
        if not isinstance(row, dict):
            return None
        pos_dict = row.get("position") if isinstance(row.get("position"), dict) else {}
        for key in ("steam_id", "guid", "identity_id", "client_id"):
            val = pos_dict.get(key)
            if val in (None, "") and key == "client_id":
                val = row.get("client_id")
            if val in (None, ""):
                continue
            try:
                skey = str(val)
            except Exception:
                continue
            header = getattr(self, "_player_net_header", {}).get(skey)
            if header:
                return header
        return None

    # ---------------- networking ----------------

    def _rx_loop(self) -> None:
        # Background thread: receive presence snapshots from the server.
        while not self._stop.is_set():
            try:
                pkt, addr = self.sock.recvfrom(65536)
            except Exception:
                continue

            if len(pkt) < HDR_SZ:
                continue

            # Server replies with: [UDP header][CTRL subheader '!BBH'][JSON payload]
            js = pkt[HDR_SZ + 4 :]  # skip control subheader
            try:
                data = json.loads(js.decode("utf-8", "ignore"))
            except Exception:
                continue

            if not data.get("ok"):
                continue

            rows = data.get("rows", [])
            try:
                self._auto_merge_enabled = bool(data.get("auto_merge_by_freq", self._auto_merge_enabled))
            except Exception:
                pass
            try:
                self._manual_merge_count = int(data.get("manual_merge_count", self._manual_merge_count))
            except Exception:
                pass
            self._rows = rows
            # Schedule UI update on main thread
            self.root.after(0, self._render_rows)

    # ---------------- rendering ----------------

    def _render_rows(self) -> None:
        # Update Clients tab
        self.tree.delete(*self.tree.get_children())
        now = time.time()

        if hasattr(self, "auto_merge_var"):
            try:
                self.auto_merge_var.set(bool(self._auto_merge_enabled))
            except Exception:
                pass
        self._update_freq_mode_button()
        if hasattr(self, "manual_merge_count_var"):
            try:
                self.manual_merge_count_var.set(f"Manual merges: {int(self._manual_merge_count)}")
            except Exception:
                self.manual_merge_count_var.set("Manual merges: ?")

        player_ids = set()
        for r in self._rows:
            pos = r.get("position")
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

        for r in self._rows:
            pos = r.get("position")
            # Skip synthetic antenna snapshot rows in the clients table; they belong only in Game tab.
            if isinstance(pos, dict) and pos.get("type") == "antenna_snapshot":
                continue
            pos_dict = pos if isinstance(pos, dict) else {}
            # Steam ID / client_id
            cid = r.get("client_id")
            if cid is None or cid == "":
                client_id = "(none)"
            else:
                client_id = str(cid)

            try:
                linked_player = bool(r.get("linked_player"))
            except Exception:
                linked_player = False
            if not linked_player and cid not in (None, ""):
                try:
                    linked_player = str(cid) in player_ids
                except Exception:
                    linked_player = False
            link_mark = "✓" if linked_player else ""

            net = r.get("net") or ""
            ssrc = r.get("ssrc")
            ptt = "TX" if r.get("ptt") else ""

            # Frequencies and scan flags
            freqs = r.get("freqs") or [0.0, 0.0, 0.0, 0.0]
            if not isinstance(freqs, (list, tuple)) or len(freqs) != 4:
                freqs = [0.0, 0.0, 0.0, 0.0]
            chan_a = self._fmt_freq(freqs[0])
            chan_b = self._fmt_freq(freqs[1])
            chan_c = self._fmt_freq(freqs[2])
            chan_d = self._fmt_freq(freqs[3])

            sc = r.get("scan_channels")
            if not isinstance(sc, (list, tuple)) or len(sc) != 4:
                sc = [False, False, False, False]
            labels = ["A", "B", "C", "D"]
            scan_list = [labels[i] for i, flag in enumerate(sc) if flag]
            scan = ",".join(scan_list)

            # Active channel (A/B/C/D)
            try:
                active_idx = int(r.get("active_channel", 0))
            except Exception:
                active_idx = 0
            active_idx = max(0, min(3, active_idx))
            active_map = ["A", "B", "C", "D"]
            active_chan = active_map[active_idx]

            addr = r.get("addr") or ""
            last_seen = float(r.get("last_seen", 0.0))
            last = f"{now - last_seen:.1f}s"

            # If linked to an in-game player that has a proximity network, apply its header to all channel nets.
            header_for_player = self._resolve_player_net_header(r)

            if header_for_player:
                labels = ["A", "B", "C", "D"]
                canon_ids = [f"{header_for_player}{self._freq_suffix(freqs[i])}" for i in range(4)]
                parts = []
                for i, label in enumerate(labels):
                    nid = canon_ids[i]
                    mark = "*" if i == active_idx else ""
                    parts.append(f"{label}:{mark}{nid}")
                net = "  ".join(parts)

            self.tree.insert(
                "",
                "end",
                values=(
                    client_id,
                    link_mark,
                    net,
                    ssrc,
                    ptt,
                    chan_a,
                    chan_b,
                    chan_c,
                    chan_d,
                    scan,
                    active_chan,
                    addr,
                    last,
                ),
        )

        # Update Networks tab based on latest rows
        self._update_network_views()
        # Ensure the server routes based on the proximity-derived network headers we display.
        self._sync_server_network_aliases()
        # Update Game tab with positions
        self._render_game_rows()

    def _rebuild_network_index(self):
        # Build networks based on spatial proximity using player/antenna ranges.
        nodes = []
        player_net_header = {}
        player_range = self._parse_range(getattr(self, "player_antenna_range_var", None))
        antenna_range = self._parse_range(getattr(self, "antenna_range_var", None))
        player_distortion_range = self._parse_range(getattr(self, "player_distortion_range_var", None))
        antenna_distortion_range = self._parse_range(getattr(self, "antenna_distortion_range_var", None))
        for r in self._rows:
            pos = r.get("position")
            if not isinstance(pos, dict):
                continue
            # Antenna snapshot payload
            if pos.get("type") == "antenna_snapshot":
                antennas = pos.get("antennas")
                if not isinstance(antennas, list):
                    continue
                for a in antennas:
                    if not isinstance(a, dict):
                        continue
                    coords = a.get("position") if isinstance(a.get("position"), dict) else {}
                    try:
                        x = float(coords.get("x"))
                        y = float(coords.get("y"))
                        z = float(coords.get("z"))
                    except Exception:
                        continue
                    lookup_keys = []
                    for key in (a.get("id"), a.get("name"), a.get("grid")):
                        if key in (None, ""):
                            continue
                        try:
                            lookup_keys.append(str(key))
                        except Exception:
                            continue
                    node_id = f"antenna:{a.get('id') or a.get('name') or a.get('grid') or len(nodes)}"
                    nodes.append(
                        {
                            "id": str(node_id),
                            "type": "antenna",
                            "name": a.get("name") or a.get("grid") or str(a.get("id") or "Antenna"),
                            "server": pos.get("server", ""),
                            "coords": (x, y, z),
                            "range": antenna_range,
                            "last_seen": float(r.get("last_seen", 0.0)),
                            "raw_id": a.get("id"),
                            "lookup_keys": lookup_keys,
                        }
                    )
                continue

            # Player position payload
            coords = pos.get("position") if isinstance(pos.get("position"), dict) else pos
            if not isinstance(coords, dict):
                continue
            try:
                x = float(coords.get("x"))
                y = float(coords.get("y"))
                z = float(coords.get("z"))
            except Exception:
                continue
            player_id = pos.get("steam_id") or pos.get("guid") or pos.get("identity_id") or r.get("client_id")
            node_id = f"player:{player_id or len(nodes)}"
            player_key = None
            lookup_keys = []
            for key in (pos.get("steam_id"), pos.get("guid"), pos.get("identity_id"), r.get("client_id")):
                if key in (None, ""):
                    continue
                try:
                    skey = str(key)
                except Exception:
                    continue
                lookup_keys.append(skey)
                if player_key is None:
                    player_key = skey
            nodes.append(
                {
                    "id": str(node_id),
                    "type": "player",
                    "name": pos.get("guid") or pos.get("steam_id") or pos.get("identity_id") or str(r.get("client_id") or "Player"),
                    "server": pos.get("server", ""),
                    "coords": (x, y, z),
                    "range": player_range,
                    "last_seen": float(r.get("last_seen", 0.0)),
                    "player_key": player_key,
                    "lookup_keys": lookup_keys,
                    "raw_id": player_id,
                }
            )

        if not nodes:
            self._network_index = {}
            self._player_net_header = {}
            self._net_id_cache = {}
            self._player_distortion = {}
            self._antenna_distortion = {}
            self._net_max_distortion = {}
            return

        # Union-find for connectivity by range.
        parent = list(range(len(nodes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i, j):
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                a, b = nodes[i], nodes[j]
                ax, ay, az = a["coords"]
                bx, by, bz = b["coords"]
                dx = ax - bx
                dy = ay - by
                dz = az - bz
                dist_sq = dx * dx + dy * dy + dz * dz
                max_range = max(float(a.get("range", 0.0)), float(b.get("range", 0.0)))
                if max_range <= 0.0:
                    continue
                if dist_sq <= max_range * max_range:
                    union(i, j)

        comps = {}
        for idx, node in enumerate(nodes):
            root = find(idx)
            comps.setdefault(root, []).append(node)

        # Reuse stable IDs where possible.
        old_cache = getattr(self, "_net_id_cache", {}) or {}
        used_ids = set()
        new_cache = {}
        idx = {}
        player_distortion = {}
        antenna_distortion = {}
        net_distortion = {}
        for comp_nodes in comps.values():
            node_ids = sorted(n["id"] for n in comp_nodes)
            key = tuple(node_ids)
            nid = old_cache.get(key)
            if not nid or nid in used_ids:
                nid = self._random_net_code(used_ids)
            used_ids.add(nid)
            new_cache[key] = nid

            # Compute per-node distortion vs the nearest antenna and the component max.
            antenna_nodes = [n for n in comp_nodes if n.get("type") == "antenna" and isinstance(n.get("coords"), tuple)]
            for n in comp_nodes:
                n["nearest_antenna_dist"] = None
                n["distortion"] = 0.0 if antenna_nodes else None
                n["net_distortion"] = None
            has_antenna = bool(antenna_nodes) and antenna_range > 0.0
            comp_max_distortion = 0.0 if has_antenna else None
            if has_antenna:
                for n in comp_nodes:
                    coords = n.get("coords")
                    if not (isinstance(coords, tuple) and len(coords) == 3):
                        continue
                    dists = []
                    for ant in antenna_nodes:
                        if ant is n:
                            continue  # do not compare an antenna to itself
                        ac = ant.get("coords")
                        if isinstance(ac, tuple) and len(ac) == 3:
                            try:
                                dists.append(math.dist(coords, ac))
                            except Exception:
                                try:
                                    dx = coords[0] - ac[0]
                                    dy = coords[1] - ac[1]
                                    dz = coords[2] - ac[2]
                                    dists.append(math.sqrt(dx * dx + dy * dy + dz * dz))
                                except Exception:
                                    continue
                    if not dists:
                        continue
                    nearest = min(dists)
                    n["nearest_antenna_dist"] = nearest
                    window = antenna_distortion_range
                    if n.get("type") == "player":
                        try:
                            window = max(player_distortion_range, antenna_distortion_range)
                        except Exception:
                            window = player_distortion_range
                    level = self._compute_distortion_level(nearest, antenna_range, window)
                    n["distortion"] = level
                    try:
                        comp_max_distortion = max(comp_max_distortion or 0.0, float(level))
                    except Exception:
                        pass
            for n in comp_nodes:
                try:
                    base_val = float(n.get("distortion")) if n.get("distortion") is not None else 0.0
                except Exception:
                    base_val = 0.0
                n["net_distortion"] = max(comp_max_distortion or 0.0, base_val) if has_antenna else None
                n["net_id"] = nid

            counts = {"player": 0, "antenna": 0}
            for n in comp_nodes:
                counts[n["type"]] = counts.get(n["type"], 0) + 1
                if n["type"] == "player" and n.get("player_key"):
                    player_net_header[n["player_key"]] = nid
                # Build quick lookup maps for distortion columns.
                info = {
                    "own": n.get("distortion"),
                    "net": n.get("net_distortion"),
                    "distance": n.get("nearest_antenna_dist"),
                    "net_id": nid,
                }
                if n["type"] == "player":
                    for key in n.get("lookup_keys") or []:
                        player_distortion[key] = info
                elif n["type"] == "antenna":
                    for key in n.get("lookup_keys") or []:
                        antenna_distortion[key] = info

            net_distortion[nid] = comp_max_distortion
            idx[nid] = {"members": comp_nodes, "counts": counts}

        self._net_id_cache = new_cache
        self._network_index = idx
        self._player_net_header = player_net_header
        self._player_distortion = player_distortion
        self._antenna_distortion = antenna_distortion
        self._net_max_distortion = net_distortion

    def _update_network_views(self) -> None:
        if not hasattr(self, "network_tree"):
            return

        self._rebuild_network_index()

        # Update networks overview
        self.network_tree.delete(*self.network_tree.get_children())
        net_ids = sorted(self._network_index.keys())

        for nid in net_ids:
            info = self._network_index[nid]
            counts = info.get("counts", {})
            p = counts.get("player", 0)
            a = counts.get("antenna", 0)
            detail = f"Players: {p}   Antennas: {a}"
            self.network_tree.insert("", "end", iid=nid, values=(nid, len(info.get("members", [])), detail))

        # Update combobox lists
        self.net_from_cb["values"] = net_ids
        self.net_to_cb["values"] = net_ids

        # Update members for currently selected network (if still present)
        sel = self.network_tree.selection()
        if sel:
            current = sel[0]
            if current in self._network_index:
                self._render_members_for_network(current)
            else:
                self.members_tree.delete(*self.members_tree.get_children())
        else:
            self.members_tree.delete(*self.members_tree.get_children())

    def _render_members_for_network(self, nid: str) -> None:
        self.members_tree.delete(*self.members_tree.get_children())
        info = getattr(self, "_network_index", {}).get(nid)
        if not info:
            return
        now = time.time()
        for m in info["members"]:
            last = f"{now - float(m.get('last_seen', 0.0)):.1f}s"
            self.members_tree.insert(
                "",
                "end",
                values=(
                    m.get("type", "").title(),
                    m.get("name", ""),
                    m.get("id", ""),
                    m.get("server", ""),
                    self._fmt_coord(m.get("coords", (None, None, None))[0] if isinstance(m.get("coords"), tuple) else None),
                    self._fmt_coord(m.get("coords", (None, None, None))[1] if isinstance(m.get("coords"), tuple) else None),
                    self._fmt_coord(m.get("coords", (None, None, None))[2] if isinstance(m.get("coords"), tuple) else None),
                    self._fmt_range(m.get("range", 0.0)),
                    last,
                ),
            )

    def _sync_server_network_aliases(self) -> None:
        """Push admin-derived network headers to the server so routing/logging matches the UI."""
        rows = getattr(self, "_rows", []) or []
        header_map = getattr(self, "_player_net_header", {}) or {}
        if not rows or not header_map:
            return

        # If the server reports zero aliases (restart or unmerge-all), drop our local cache.
        try:
            if int(getattr(self, "_manual_merge_count", 0)) == 0 and getattr(self, "_last_sent_net_alias", None):
                self._last_sent_net_alias = {}
        except Exception:
            pass

        merges = {}
        for r in rows:
            header = self._resolve_player_net_header(r)
            if not header:
                continue

            freqs = r.get("freqs") or [0.0, 0.0, 0.0, 0.0]
            if not isinstance(freqs, (list, tuple)) or len(freqs) != 4:
                try:
                    freqs = list(freqs)
                except Exception:
                    freqs = []
                if len(freqs) < 4:
                    freqs = (freqs + [0.0, 0.0, 0.0, 0.0])[:4]
                else:
                    freqs = freqs[:4]

            net_ids = r.get("net_ids")
            if not isinstance(net_ids, (list, tuple)) or len(net_ids) != 4:
                continue

            for i, src in enumerate(net_ids):
                src = (src or "").strip()
                if not src:
                    continue
                suffix = self._freq_suffix(freqs[i] if i < len(freqs) else 0.0)
                dst = f"{header}{suffix}"
                if not dst or src == dst:
                    continue
                merges[src] = dst

        if not merges:
            return

        cache = getattr(self, "_last_sent_net_alias", {}) or {}
        for src, dst in merges.items():
            if cache.get(src) == dst:
                continue
            self._send_net_merge(src, dst)
            cache[src] = dst
        self._last_sent_net_alias = cache

    def _render_game_rows(self) -> None:
        if not hasattr(self, "game_tree"):
            return
        self.game_tree.delete(*self.game_tree.get_children())
        if hasattr(self, "antenna_tree"):
            self.antenna_tree.delete(*self.antenna_tree.get_children())
        now = time.time()
        for r in self._rows:
            pos = r.get("position")
            if not isinstance(pos, dict):
                continue

            # Antenna snapshot payload: {type:"antenna_snapshot", antennas:[...]}
            if pos.get("type") == "antenna_snapshot":
                antennas = pos.get("antennas")
                if isinstance(antennas, list) and hasattr(self, "antenna_tree"):
                    for a in antennas:
                        if not isinstance(a, dict):
                            continue
                        coords = a.get("position") if isinstance(a.get("position"), dict) else {}
                        x = self._fmt_coord(coords.get("x"))
                        y = self._fmt_coord(coords.get("y"))
                        z = self._fmt_coord(coords.get("z"))
                        dist_txt = ""
                        try:
                            lookup = getattr(self, "_antenna_distortion", {}) or {}
                            dist_info = None
                            for key in (a.get("id"), a.get("name"), a.get("grid")):
                                if key in (None, ""):
                                    continue
                                try:
                                    dist_info = lookup.get(str(key))
                                except Exception:
                                    dist_info = None
                                if dist_info:
                                    break
                            if dist_info:
                                # Antenna rows should show only their own edge distortion,
                                # not the network-wide max driven by other nodes.
                                dist_txt = self._fmt_distortion(dist_info.get("own"), None)
                        except Exception:
                            dist_txt = ""
                        last = f"{now - float(r.get('last_seen', 0.0)):.1f}s"
                        self.antenna_tree.insert(
                            "",
                            "end",
                            values=(
                                pos.get("server", ""),
                                a.get("name", ""),
                                a.get("grid", ""),
                                a.get("id", ""),
                                x,
                                y,
                                z,
                                dist_txt,
                                last,
                            ),
                        )
                continue

            # Player position payload: {guid, steam_id, identity_id, position:{x,y,z}}
            coords = pos.get("position") if isinstance(pos.get("position"), dict) else pos
            if not isinstance(coords, dict):
                continue
            guid = pos.get("guid") or r.get("client_id") or ""
            steam_id = pos.get("steam_id") or r.get("client_id") or ""
            identity_id = pos.get("identity_id") or ""
            x = self._fmt_coord(coords.get("x"))
            y = self._fmt_coord(coords.get("y"))
            z = self._fmt_coord(coords.get("z"))
            dist_txt = ""
            try:
                lookup = getattr(self, "_player_distortion", {}) or {}
                dist_info = None
                for key in (pos.get("steam_id"), pos.get("guid"), pos.get("identity_id"), r.get("client_id")):
                    if key in (None, ""):
                        continue
                    try:
                        dist_info = lookup.get(str(key))
                    except Exception:
                        dist_info = None
                    if dist_info:
                        break
                if dist_info:
                    dist_txt = self._fmt_distortion(dist_info.get("own"), dist_info.get("net"))
            except Exception:
                dist_txt = ""
            last = f"{now - float(r.get('last_seen', 0.0)):.1f}s"
            self.game_tree.insert(
                "",
                "end",
                values=(pos.get("server", ""), guid, steam_id, identity_id, x, y, z, dist_txt, last),
            )

    # ---------------- networks tab actions ----------------

    def _on_network_select(self, event=None) -> None:
        sel = self.network_tree.selection()
        if not sel:
            return
        nid = sel[0]
        self._render_members_for_network(nid)

    def _send_net_merge(self, src: str, dst: str) -> None:
        """Send a CTRL_ADMIN_NET_MERGE command to the server."""
        info = {"from": src, "into": dst}
        payload = json.dumps(info).encode("utf-8")
        seq = self.seq.next()
        try:
            hdr = pack_hdr(VER, MT_CTRL, seq, now_ts48(), 0)
            ctrl_hdr = struct.pack("!BH", CTRL_ADMIN_NET_MERGE, len(payload))
            pkt = hdr + ctrl_hdr + payload
            self.sock.sendto(pkt, (self.host, self.port))
        except Exception as e:
            self.merge_status.set(f"Send error: {e}")

    def _send_auto_merge_toggle(self, enabled: bool) -> None:
        """Send a toggle for frequency-based auto merging."""
        payload = json.dumps({"auto_merge": bool(enabled)}).encode("utf-8")
        seq = self.seq.next()
        try:
            hdr = pack_hdr(VER, MT_CTRL, seq, now_ts48(), 0)
            ctrl_hdr = struct.pack("!BH", CTRL_ADMIN_NET_AUTOMERGE, len(payload))
            pkt = hdr + ctrl_hdr + payload
            self.sock.sendto(pkt, (self.host, self.port))
        except Exception as e:
            self.merge_status.set(f"Send error: {e}")

    def _on_auto_merge_toggle(self) -> None:
        enabled = bool(self.auto_merge_var.get())
        self._send_auto_merge_toggle(enabled)
        state = "ON" if enabled else "OFF"
        self.merge_status.set(f"Requested freq-only mode {state} (ignores net headers)")
        self._update_freq_mode_button()

    def _on_freq_mode_click(self) -> None:
        # Convenience button to flip the auto-merge-by-frequency toggle.
        try:
            new_state = not bool(self.auto_merge_var.get())
        except Exception:
            new_state = False
        try:
            self.auto_merge_var.set(new_state)
        except Exception:
            pass
        self._on_auto_merge_toggle()

    def _on_unmerge_all(self) -> None:
        seq = self.seq.next()
        try:
            hdr = pack_hdr(VER, MT_CTRL, seq, now_ts48(), 0)
            ctrl_hdr = struct.pack("!BH", CTRL_ADMIN_NET_UNMERGE_ALL, 0)
            pkt = hdr + ctrl_hdr
            self.sock.sendto(pkt, (self.host, self.port))
            self.merge_status.set("Requested un-merge for all networks")
        except Exception as e:
            self.merge_status.set(f"Send error: {e}")

    def _on_merge_click(self) -> None:
        src = (self.net_from_var.get() or "").strip()
        dst = (self.net_to_var.get() or "").strip()

        if not src or not dst:
            self.merge_status.set("Select both networks to merge.")
            return
        if src == dst:
            self.merge_status.set("From and Into must be different.")
            return

        self._send_net_merge(src, dst)
        self.merge_status.set(f"Requested merge {src} → {dst} (server-side)")

    # ---------------- update tab actions ----------------

    def _on_browse_update(self):
        path = filedialog.askopenfilename(
            title="Select client update (.exe)",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.update_path_var.set(path)

    def _on_update_click(self):
        path = (self.update_path_var.get() or "").strip()
        if not path or not os.path.isfile(path):
            self.update_status.set("Select a valid .exe first.")
            return
        self.update_status.set("Uploading update�?�")
        threading.Thread(target=self._upload_update_file, args=(path,), daemon=True).start()

    def _upload_update_file(self, path: str):
        upload_host = self.host if self.host and self.host != "0.0.0.0" else "127.0.0.1"
        url = f"http://{upload_host}:{UPDATE_HTTP_PORT}/upload"
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self.root.after(0, lambda: self.update_status.set(f"Read error: {e}"))
            return

        def _set(msg: str):
            try:
                self.update_status.set(msg)
            except Exception:
                pass

        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("X-Filename", name)
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp_body = resp.read()
            try:
                obj = json.loads(resp_body.decode("utf-8", "ignore"))
            except Exception:
                obj = {"ok": False, "reason": "Bad response"}
            if obj.get("ok"):
                upd = obj.get("update") or {}
                size = upd.get("size") or len(data)
                mb = float(size) / (1024 * 1024)
                msg = f"Uploaded {name} ({mb:.2f} MB). Clients will be prompted on connect."
            else:
                msg = f"Upload failed: {obj.get('reason', 'Unknown error')}"
        except urllib.error.HTTPError as e:
            msg = f"Upload failed: HTTP {e.code}"
        except Exception as e:
            msg = f"Upload failed: {e}"

        self.root.after(0, lambda: _set(msg))

    # ---------------- poll / close ----------------

    def _poll(self) -> None:
        # Send a CTRL_PRESENCE poll frame to the UDP server.
        seq = self.seq.next()
        try:
            hdr = pack_hdr(VER, MT_CTRL, seq, now_ts48(), 0)
            # Minimal CTRL subheader: code, reserved, length (0)
            sub = struct.pack("!BBH", CTRL_PRESENCE, 0, 0)
            msg = hdr + sub
        except Exception as e:
            self.status.set(f"Build error: {e}")
            return

        try:
            self.sock.sendto(msg, (self.host, self.port))
            self.status.set("Polling…")
        except Exception as e:
            self.status.set(f"Send error: {e}")

        if not self._stop.is_set():
            self.root.after(POLL_MS, self._poll)

    def on_close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--udp_host", default="127.0.0.1")
    p.add_argument("--udp_port", type=int, default=8765)
    a = p.parse_args()

    root = tk.Tk()
    app = AdminApp(root, a.udp_host, a.udp_port)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
