# app.py — Hybrid client (UDP + Connection tab + Overlay)
# Clean indentation; safe to drop into app/app.py
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading, time, os, random, json, hashlib, urllib.request, urllib.parse, shutil, subprocess
import sys, ctypes
from ctypes import wintypes
from collections import deque

print('[CLIENT][BUILD] app HYBRID-UDP-OVERLAY loaded')

# --- Project modules ---
from udp_client import UdpVoiceClient                           # UDP link
from udp_protocol import CTRL_UPDATE_OFFER, CTRL_UPDATE_RESPONSE
from .config_io import load_user_config, save_user_config       # user config
from .devices import scan_filtered_devices                      # device listing
from .hotkeys import GlobalKeyListener, have_pynput  # global PTT
from .sounds import SoundPlayer                                 # UI sounds
from .audio_io import AudioEngine                               # audio I/O
from .effects import EdgeEffects                                # SFX (future)
from .overlay_ui import OverlayWindow                           # overlay UI

# Optional deps for mic VU test
try:
    import sounddevice as sd
except Exception:
    sd = None
try:
    import numpy as _np
except Exception:
    _np = None

APP_TITLE = "Colony Radio v0.6.8"
APP_VERSION = "0.6.8"
DEFAULT_PORT = "8765"
AUDIO_RATE = 48000
AUDIO_BLOCK = 480  # 10 ms at 48 kHz
CHANNEL_D_LOCKED_FREQ = "111.1"  # Channel D is fixed to this frequency

# ------------------- helpers -------------------
def _snap_10(v: int) -> int:
    return max(0, min(100, (int(v) // 10) * 10))

def _snap_10_min30(v: int) -> int:
    return max(30, _snap_10(v))

class DebugAudio:
    """Local debug player using pygame.mixer when available (optional)."""
    USER_TEST_DIR = r"C:\Users\lscott\Desktop\SE-Radio-Client-v0.1\Audio\Test"

    def __init__(self):
        self.ok = False
        self.multi_ok = False
        self._channels = []
        self.test_files = []
        self._init_mixer()
        self.test_files = self._discover_test_files()
        if self.ok:
            try:
                import pygame
                for i in range(4):
                    self._channels.append(pygame.mixer.Channel(i))
            except Exception:
                pass

    def _init_mixer(self):
        try:
            import pygame
            pygame.mixer.init(frequency=44100, channels=2)
            self.ok = True
            self.multi_ok = True
        except Exception:
            self.ok = False
            self.multi_ok = False

    def _discover_test_files(self):
        candidates = []
        paths_to_try = [self.USER_TEST_DIR]
        try:
            app_dir = os.path.dirname(__file__)
            repo_root = os.path.dirname(app_dir)  # parent of 'app'
            fallback = os.path.join(repo_root, "Audio", "Test")
            paths_to_try.append(fallback)
        except Exception:
            pass
        exts = [".ogg", ".wav", ".mp3"]
        for base in paths_to_try:
            if not base or not os.path.isdir(base):
                continue
            for i in range(1, 6):
                for ext in exts:
                    p = os.path.join(base, f"test{i}{ext}")
                    if os.path.isfile(p):
                        candidates.append(p)
                        break
            if candidates:
                break
        return candidates

    def can_play(self):
        return self.ok and len(self.test_files) > 0

    def play_on_channel(self, index: int):
        if not self.can_play():
            return
        import pygame
        fpath = random.choice(self.test_files)
        _, ext = os.path.splitext(fpath.lower())
        try:
            if ext in (".ogg", ".wav") and self.multi_ok and self._channels:
                snd = pygame.mixer.Sound(fpath)
                ch = self._channels[index % len(self._channels)]
                ch.play(snd)
            else:
                pygame.mixer.music.load(fpath)
                pygame.mixer.music.play()
        except Exception:
            pass


class InputDebugWindow:
    """Simple Tk window that mirrors every captured input token for troubleshooting."""

    def __init__(self, app_ref, on_close=None, on_ignore=None):
        self.app = app_ref
        self.on_close = on_close
        self.on_ignore = on_ignore
        self.top = None
        self.tree = None
        self.state_var = None
        self._ignore_btn = None

    def is_open(self):
        return bool(self.top and self.top.winfo_exists())

    def focus(self):
        if self.is_open():
            try:
                self.top.deiconify()
                self.top.lift()
                self.top.focus_force()
            except Exception:
                pass

    def open(self):
        if self.is_open():
            self.focus()
            return
        self.top = tk.Toplevel(self.app.root)
        self.top.title("Input Debugger")
        self.top.geometry("760x380")
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        palette = getattr(self.app, "_palette", {}) or {}
        muted = palette.get("muted", "#555")

        ttk.Label(
            self.top,
            text="Live view of keyboard and mouse events seen by the keybind system.",
            foreground=muted
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 6))

        self.state_var = tk.StringVar(value="Pressed now: None")
        ttk.Label(self.top, textvariable=self.state_var, foreground=muted).grid(
            row=1, column=0, sticky="w", padx=12, pady=(0, 8)
        )
        btn_frame = ttk.Frame(self.top)
        btn_frame.grid(row=1, column=1, sticky="e", padx=12, pady=(0, 8))
        ttk.Button(btn_frame, text="Clear", command=self.clear).grid(row=0, column=0, padx=(0,6))
        self._ignore_btn = ttk.Button(btn_frame, text="Ignore Input", command=self._handle_ignore)
        self._ignore_btn.grid(row=0, column=1)
        self._show_ignored_btn = ttk.Button(btn_frame, text="Show Ignored Inputs", command=self._show_ignored)
        self._show_ignored_btn.grid(row=0, column=2, padx=(6,0))

        cols = ("time", "source", "token", "action", "detail")
        self.tree = ttk.Treeview(self.top, columns=cols, show="headings", height=14)
        self.tree.heading("time", text="Time")
        self.tree.heading("source", text="Source")
        self.tree.heading("token", text="Token")
        self.tree.heading("action", text="Action")
        self.tree.heading("detail", text="Detail")
        self.tree.column("time", width=80, anchor="w")
        self.tree.column("source", width=140, anchor="w")
        self.tree.column("token", width=140, anchor="w")
        self.tree.column("action", width=80, anchor="w")
        self.tree.column("detail", width=300, anchor="w")
        self.tree.grid(row=2, column=0, columnspan=1, sticky="nsew", padx=(12, 0), pady=(0, 12))

        scrolly = ttk.Scrollbar(self.top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrolly.set)
        scrolly.grid(row=2, column=1, sticky="ns", padx=(0, 12), pady=(0, 12))

        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(2, weight=1)
        self.focus()

    def clear(self):
        if not self.tree:
            return
        try:
            for child in self.tree.get_children():
                self.tree.delete(child)
            if self.state_var:
                self.state_var.set("Pressed now: None")
        except Exception:
            pass

    def close(self):
        try:
            if self.top:
                self.top.destroy()
        except Exception:
            pass
        self.top = None
        if callable(self.on_close):
            try:
                self.on_close()
            except Exception:
                pass

    def record_events(self, events, pressed_tokens):
        if not self.is_open() or not self.tree:
            return
        try:
            for ev in events:
                tok = ev.get("token", "") or ""
                values = (
                    ev.get("time", ""),
                    ev.get("origin", ""),
                    tok,
                    ev.get("action", ""),
                    ev.get("detail", ""),
                )
                self.tree.insert("", "end", values=values)
            # Keep the log bounded so the UI stays responsive
            children = self.tree.get_children()
            if len(children) > 400:
                for child in children[: len(children) - 400]:
                    self.tree.delete(child)
            self.tree.yview_moveto(1.0)
            joined = ", ".join(pressed_tokens) if pressed_tokens else "None"
            if self.state_var:
                self.state_var.set(f"Pressed now: {joined}")
        except Exception:
            pass

    def get_selected_tokens(self):
        if not self.tree:
            return []
        toks = []
        try:
            for iid in self.tree.selection():
                vals = self.tree.item(iid, "values")
                if len(vals) >= 3 and vals[2]:
                    toks.append(vals[2])
        except Exception:
            return []
        return toks

    def _handle_ignore(self):
        toks = self.get_selected_tokens()
        if not toks:
            return
        cb = self.on_ignore
        if callable(cb):
            try:
                cb(toks)
            except Exception:
                pass

    def _show_ignored(self):
        try:
            if not hasattr(self.app, "_ignored_tokens"):
                return
            tokens = sorted(self.app._ignored_tokens)
        except Exception:
            tokens = []
        if not tokens:
            messagebox.showinfo("Ignored Inputs", "No inputs are currently ignored.", parent=self.top)
            return
        # Simple chooser to unignore
        dlg = tk.Toplevel(self.top)
        dlg.title("Ignored Inputs")
        dlg.transient(self.top)
        dlg.grab_set()
        ttk.Label(dlg, text="Select inputs to unignore:").grid(row=0, column=0, sticky="w", padx=12, pady=(12,6))
        listbox = tk.Listbox(dlg, selectmode="extended", width=40, height=min(12, len(tokens)))
        for t in tokens:
            listbox.insert("end", t)
        listbox.grid(row=1, column=0, padx=12, pady=(0,10), sticky="nsew")
        btns = ttk.Frame(dlg)
        btns.grid(row=2, column=0, sticky="e", padx=12, pady=(0,12))
        result = {"selection": None}
        def _apply():
            sel = listbox.curselection()
            result["selection"] = [listbox.get(i) for i in sel]
            dlg.destroy()
        ttk.Button(btns, text="Unignore Selected", command=_apply).grid(row=0, column=0, padx=(0,6))
        ttk.Button(btns, text="Close", command=dlg.destroy).grid(row=0, column=1)
        dlg.columnconfigure(0, weight=1)
        dlg.wait_window()
        selection = result.get("selection") or []
        if selection and callable(self.on_ignore):
            try:
                self.on_ignore([], unignore=selection)
            except Exception:
                pass


class App:
    # === Frequency & PTT reporting (added) ==================================
    def _send_chan_update(self):
        """Send current channel selection and frequency mapping to server."""
        try:
            freqs = [
                float(self.chan_a_var.get()) if hasattr(self, "chan_a_var") else float(self.freq_a_var.get()) if hasattr(self, "freq_a_var") else float(self.chan_freqs[0]),
                float(self.chan_b_var.get()) if hasattr(self, "chan_b_var") else float(self.freq_b_var.get()) if hasattr(self, "freq_b_var") else float(self.chan_freqs[1]),
                float(self.chan_c_var.get()) if hasattr(self, "chan_c_var") else float(self.freq_c_var.get()) if hasattr(self, "freq_c_var") else float(self.chan_freqs[2]),
                float(self.chan_d_var.get()) if hasattr(self, "chan_d_var") else float(self.freq_d_var.get()) if hasattr(self, "freq_d_var") else float(self.chan_freqs[3]),
            ]
        except Exception:
            try:
                freqs = [float(x) for x in getattr(self, "chan_freqs", [0.0,0.0,0.0,0.0])]
            except Exception:
                freqs = [0.0,0.0,0.0,0.0]

        # Determine active channel index
        idx = 0
        for cand in ("active_channel_idx", "active_idx", "active_chan_idx", "active_channel"):
            if hasattr(self, cand):
                try:
                    idx = int(getattr(self, cand))
                    break
                except Exception:
                    pass
        try:
            scan, _ = self._current_scan_state()
        except Exception:
            scan = bool(getattr(self, "scan_enabled", False))

        try:
            if hasattr(self, "net") and hasattr(self.net, "set_active_channel"):
                self.net.set_active_channel(idx, freqs, scan)
        except Exception:
            pass

    def _begin_tx(self):
        """Notify server of PTT start with exact TX frequency (derived from active channel)."""
        try:
            # Assemble freq list similar to _send_chan_update
            try:
                freqs = [
                    float(self.chan_a_var.get()) if hasattr(self, "chan_a_var") else float(self.freq_a_var.get()) if hasattr(self, "freq_a_var") else float(self.chan_freqs[0]),
                    float(self.chan_b_var.get()) if hasattr(self, "chan_b_var") else float(self.freq_b_var.get()) if hasattr(self, "freq_b_var") else float(self.chan_freqs[1]),
                    float(self.chan_c_var.get()) if hasattr(self, "chan_c_var") else float(self.freq_c_var.get()) if hasattr(self, "freq_c_var") else float(self.chan_freqs[2]),
                    float(self.chan_d_var.get()) if hasattr(self, "chan_d_var") else float(self.freq_d_var.get()) if hasattr(self, "freq_d_var") else float(self.chan_freqs[3]),
                ]
            except Exception:
                freqs = [float(x) for x in getattr(self, "chan_freqs", [0.0,0.0,0.0,0.0])]

            # Active index
            idx = 0
            for cand in ("active_channel_idx", "active_idx", "active_chan_idx", "active_channel"):
                if hasattr(self, cand):
                    try:
                        idx = int(getattr(self, cand))
                        break
                    except Exception:
                        pass
            idx = max(0, min(3, idx))
            tx_freq = float(freqs[idx])
            if hasattr(self, "net") and hasattr(self.net, "send_ptt"):
                self.net.send_ptt("start", tx_freq)
        except Exception:
            pass

    def _end_tx(self):
        """Notify server of PTT stop."""
        try:
            if hasattr(self, "net") and hasattr(self.net, "send_ptt"):
                self.net.send_ptt("stop", None)
        except Exception:
            pass

    # ---------- UDP logging helper ----------
    def _udp_log(self, msg: str):
        """Receive log lines from UdpVoiceClient and mirror to console/status."""
        try:
            print(msg)
        except Exception:
            pass
        try:
            if hasattr(self, "status_text") and isinstance(msg, str):
                if "[UDP][TX]" in msg or "[UDP][RX]" in msg:
                    # Show last UDP activity in status bar without spamming UI too hard
                    self.status_text.set(msg)
        except Exception:
            pass

    def start(self):
        """Public entry point for starting the audio loop."""
        return self._start_audio_loop()

    def __init__(self, root):
        # Core Tk
        self.root = root
        root.title(APP_TITLE)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.ui_theme = tk.StringVar(value="light")
        self._palette = {}
        self._style = ttk.Style()
        self._font_body = ("Segoe UI", 10)
        self._font_heading = ("Segoe UI Semibold", 10)
        try:
            self._base_theme = self._style.theme_use()
        except Exception:
            self._base_theme = "default"
        self._scales = []
        self._muted_labels = []

        # --- Core state ---
        self.running = False
        self.ptt = tk.BooleanVar(value=False)
        self.ptt_mode = tk.StringVar(value="hold")
        self.input_dev = tk.StringVar(value="Default")
        self.output_dev = tk.StringVar(value="Default")
        # UI sound effects volume (0.0-2.0 gain mapped from 0-100 slider)
        self.sfx_gain = tk.DoubleVar(value=1.0)
        self.sfx_slider_var = tk.DoubleVar(value=self._sfx_slider_from_gain(self.sfx_gain.get()))

        # --- Channels A-C ---
        self.chan_vars = [
            tk.StringVar(value="102.3"),
            tk.StringVar(value="553.4"),
            tk.StringVar(value="000.0")
        ]
        self.active_chan = tk.IntVar(value=0)
        self.scan_vars = [
            tk.BooleanVar(value=False),
            tk.BooleanVar(value=False),
            tk.BooleanVar(value=False)
        ]
        self.chan_vol_vars = [
            tk.IntVar(value=100),
            tk.IntVar(value=100),
            tk.IntVar(value=100)
        ]

        # --- Channel D (locked freq/scan; adjustable volume >=30%) ---
        self.chan_d_var = tk.StringVar(value=CHANNEL_D_LOCKED_FREQ)
        self.scan_d_var = tk.BooleanVar(value=True)
        self.chan_d_vol_var = tk.IntVar(value=50)

        # --- Frequency change debounce (client -> server joins) ---
        self._freq_debounce_after_id = None
        self._freq_debounce_ms = 400  # ms

        # --- Combos (allow up to 3 per action) ---
        self.next_combos = [frozenset(["F7"])]
        self.prev_combos = [frozenset(["F6"])]
        self.vol_up_combos = [frozenset(["F8"])]
        self.vol_down_combos = [frozenset(["F9"])]
        self.ptt_combos = [frozenset(["F1"])]
        self.chan_a_combos = []
        self.chan_b_combos = []
        self.chan_c_combos = []
        self.chan_d_combos = []
        self._edge_next_prev = {"next": False, "prev": False}
        self._edge_vol = {"up": False, "down": False}
        self._edge_chan_select = [False, False, False, False]
        self._pressed = set(); self._pressed_global = set()
        self._combo_active_prev = False
        self._input_refresh_event = threading.Event()
        self._input_refresh_lock = threading.Lock()
        self._input_refresh_last_source = ""
        self._input_refresh_poll_ms = 16
        self._last_input_tokens = frozenset()
        self._last_udp_ptt = None
        self._waiting_bind = False; self._waiting_bind_for = None; self._bind_candidate = frozenset()
        self._bind_mode = None; self._bind_replace_index = None
        self._bind_seen_input = False
        self._bind_last_non_empty = frozenset()
        self._bind_use_global = False

        # Input debug viewer (captures every token seen by the keybind system)
        self._input_debugger = None
        self._input_debug_enabled = False
        self._input_debug_lock = threading.Lock()
        self._input_debug_buffer = deque(maxlen=400)
        self._ignored_tokens = set()
        self.joystick_enabled = tk.BooleanVar(value=True)

        # --- Connection state ---
        self.server_ip = tk.StringVar(value="68.44.24.179")
        self.server_port = tk.StringVar(value=DEFAULT_PORT)
        self.connected = tk.BooleanVar(value=False)
        self.network = tk.StringVar(value="default")
        # Callsign used as UDP nick; simple default if user hasn't set one
        self.callsign_var = tk.StringVar(value="Client")
        # Optional Steam GUID used as SSRC override
        self.steam_ssrc_var = tk.StringVar(value="")
        self.my_ssrc = None
        self._last_update_tag = None

        # --- RX Active tracking for overlay label ---
        self.rx_active = set()            # set of indices 0..3 currently receiving
        self.rx_last_until = [0,0,0,0]    # epoch ms when last activity should expire
        self.rx_hang_ms = 1000            # 1s hang

        # --- Debug RX system ---
        self.debug_rx_enabled = tk.BooleanVar(value=False)
        # Debug/test: when enabled, ask server to route our TX audio back to us.
        self.loopback_enabled = tk.BooleanVar(value=False)
        self._debug_thread = None
        self._debug_stop = threading.Event()
        self._debug_audio = DebugAudio()
        self._rx_queue = deque(maxlen=200)
        self._rx_lock = threading.Lock()
        self._rx_lamp_queue = []
        self._rx_lamp_lock = threading.Lock()
        self._rx_started = False
        self._rx_last_frame = None  # last successfully mixed frame for underrun concealment
        self._rx_last_repeat = 0    # how many times we've reused the last frame in a row
        self.last_rx_ts = 0.0
        self.rx_active_recent_ts = 0.0

        # Load config
        self._load_user_config_all()
        self._palette = self._get_palette(self.ui_theme.get())
        self.chan_d_vol_var.set(_snap_10_min30(self.chan_d_vol_var.get()))

        # Audio & FX
        self.engine = AudioEngine(samplerate=AUDIO_RATE, blocksize=AUDIO_BLOCK)
        self.effects = EdgeEffects()
        self.sounds = SoundPlayer()
        self._apply_sfx_volume(save=False)

        # UI
        self._build_ui()
        self._apply_theme(self.ui_theme.get())
        self._update_active_label()
        self._update_connection_indicator()
        self._update_audible_hint()

        # Devices
        self._populate_devices()

        # Key hooks
        self.root.bind("<KeyPress>", self._on_key_press, add="+")
        self.root.bind("<KeyRelease>", self._on_key_release, add="+")
        self.root.bind("<ButtonPress>", self._on_mouse_press, add="+")
        self.root.bind("<ButtonRelease>", self._on_mouse_release, add="+")

        # Global hotkeys
        self.global_keys = GlobalKeyListener(self)
        try: self.global_keys.set_gamepad_polling(bool(self.joystick_enabled.get()))
        except Exception: pass
        try: self.sounds.ensure_init()
        except Exception: pass
        try: self.global_keys.start()
        except Exception: pass

        self.worker_thread = None

        # Overlay (preview UI)
        self.overlay = OverlayWindow(self, open_immediately=True)
        self.root.bind("<F10>", lambda e: self.overlay.toggle())

        # Debounced notify while editing channel frequencies (A–D)
        try:
            for _v in list(self.chan_vars) + [self.chan_d_var]:
                _v.trace_add("write", lambda *_: self._on_freq_var_changed())
        except Exception:
            pass

        # Input recompute pump (keeps global hotkey threads off the Tk thread)
        self.root.after(self._input_refresh_poll_ms, self._drain_input_refresh)

        # Tickers
        self.root.after(100, self._tick_ui)
        self.root.after(150, self._tick_rx_expire)

    # ---------------- UI ----------------
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        self.nb = ttk.Notebook(self.root)
        self.nb.grid(row=0, column=0, sticky="nsew")

        self.tab_channels = ttk.Frame(self.nb)
        self.tab_conn = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.tab_keybinds = ttk.Frame(self.nb)
        self.tab_debug = ttk.Frame(self.nb)
        self.nb.add(self.tab_channels, text="Channels")
        self.nb.add(self.tab_conn, text="Connection")
        self.nb.add(self.tab_settings, text="Settings")
        self.nb.add(self.tab_keybinds, text="Keybinds")
        self.nb.add(self.tab_debug, text="Debug")

        self._build_channels_tab(self.tab_channels)
        self._build_connection_tab(self.tab_conn)
        self._build_settings_tab(self.tab_settings)
        self._build_keybinds_tab(self.tab_keybinds)
        self._build_debug_tab(self.tab_debug)

        status_frame = ttk.Frame(self.root, padding=(8, 4))
        status_frame.grid(row=1, column=0, sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        self.conn_indicator = ttk.Label(status_frame, text="● Disconnected", foreground=self._palette.get("bad", "#b11"))
        self.conn_indicator.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.status_text = tk.StringVar(value="Idle")
        self.status = ttk.Label(status_frame, textvariable=self.status_text, foreground=self._palette.get("fg"))
        self.status.grid(row=0, column=1, sticky="e")

    def _build_channels_tab(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1); parent.rowconfigure(0, weight=1)

        ttk.Label(frm, text="Input Device:").grid(row=0, column=0, sticky="w")
        self.in_combo = ttk.Combobox(frm, textvariable=self.input_dev, values=["Scanning…"], width=50)
        self.in_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)

        ttk.Label(frm, text="Output Device:").grid(row=1, column=0, sticky="w")
        self.out_combo = ttk.Combobox(frm, textvariable=self.output_dev, values=["Scanning…"], width=50)
        self.out_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)

        self.rescan_btn = ttk.Button(frm, text="Rescan Devices", command=self._populate_devices)
        self.rescan_btn.grid(row=0, column=3, rowspan=2, padx=6, sticky="ns")

        chan_frame = ttk.LabelFrame(frm, text="Channels", style="Card.TLabelframe")
        chan_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 6))
        for c in range(6): chan_frame.columnconfigure(c, weight=1)

        self.audible_hint = ttk.Label(chan_frame, text="", foreground=self._palette.get("muted", "#555"))
        self.audible_hint.grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 4))
        self._muted_labels.append(self.audible_hint)

        def _mk_block(block_idx, label, var, scan_var, vol_var, start_row):
            ttk.Label(chan_frame, text=f"{label} Channel (XXX.X MHz):").grid(row=start_row, column=0, sticky="w")
            e = ttk.Entry(chan_frame, textvariable=var, width=8, justify="right", validate="key")
            import re
            vcmd = (self.root.register(lambda s: bool(re.match(r"^\d{0,3}(\.\d?)?$", s or ""))), "%P")
            e.configure(validatecommand=vcmd)
            e.grid(row=start_row, column=1, sticky="w"); ttk.Label(chan_frame, text="MHz").grid(row=start_row, column=2, sticky="w")
            e.bind("<FocusOut>", lambda _e=None: self._notify_server())
            e.bind("<Return>",   lambda _e=None: self._notify_server())
            cb = ttk.Checkbutton(chan_frame, text="Scan", variable=scan_var, command=self._on_scan_changed)
            cb.grid(row=start_row, column=3, sticky="w", padx=(8,0))

            ttk.Label(chan_frame, text="Volume").grid(row=start_row+1, column=0, sticky="e")
            s = tk.Scale(chan_frame, from_=0, to=100, orient="horizontal", variable=vol_var,
                         resolution=10, showvalue=True, length=260)
            s.grid(row=start_row+1, column=1, columnspan=3, sticky="ew", padx=(6, 6))
            self._scales.append(s)
            def _snap_and_save(_=None):
                v = int(vol_var.get()); snapped = _snap_10(v)
                if snapped != v: vol_var.set(snapped)
                self._save_user_config_all(); self._update_audible_hint(); self._update_active_label()
                try: self.sounds.play_volume()
                except Exception: pass
            s.bind("<ButtonRelease-1>", _snap_and_save)

            if block_idx < 2:
                sep = ttk.Separator(chan_frame, orient="horizontal")
                sep.grid(row=start_row+2, column=0, columnspan=6, sticky="ew", pady=(6,4))

        row = 1
        _mk_block(0, "A", self.chan_vars[0], self.scan_vars[0], self.chan_vol_vars[0], row); row += 3
        _mk_block(1, "B", self.chan_vars[1], self.scan_vars[1], self.chan_vol_vars[1], row); row += 3
        _mk_block(2, "C", self.chan_vars[2], self.scan_vars[2], self.chan_vol_vars[2], row); row += 3

        # Channel D (locked UI except volume enabled with 30% floor)
        ttk.Label(chan_frame, text="D Channel (XXX.X MHz):").grid(row=row, column=0, sticky="w")
        e_d = ttk.Entry(chan_frame, textvariable=self.chan_d_var, width=8, justify="right", state="disabled")
        e_d.grid(row=row, column=1, sticky="w")
        ttk.Label(chan_frame, text="MHz").grid(row=row, column=2, sticky="w")
        cb_d = ttk.Checkbutton(chan_frame, text="Scan", variable=self.scan_d_var, state="disabled")
        cb_d.grid(row=row, column=3, sticky="w", padx=(8,0))

        ttk.Label(chan_frame, text="Volume").grid(row=row+1, column=0, sticky="e")
        s_d = tk.Scale(chan_frame, from_=30, to=100, orient="horizontal",
                       variable=self.chan_d_vol_var, resolution=10, showvalue=True, length=260)
        s_d.grid(row=row+1, column=1, columnspan=3, sticky="ew", padx=(6, 6))
        self._scales.append(s_d)
        def _snap_and_save_d(_=None):
            v = int(self.chan_d_vol_var.get())
            snapped = _snap_10_min30(v)
            if snapped != v: self.chan_d_vol_var.set(snapped)
            self._save_user_config_all(); self._update_audible_hint(); self._update_active_label()
            try: self.sounds.play_volume()
            except Exception: pass
        s_d.bind("<ButtonRelease-1>", _snap_and_save_d)

        row += 3

        # Active + cycle buttons (A–D cycling)
        self.active_label = ttk.Label(chan_frame, text="Active: A (102.3 MHz)")
        self.active_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4,0))
        ttk.Button(chan_frame, text="Prev ◀", command=self._cycle_prev).grid(row=row, column=2, sticky="e", padx=4)
        ttk.Button(chan_frame, text="Next ▶", command=self._cycle_next).grid(row=row, column=3, sticky="w")

        for c in range(4):
            frm.columnconfigure(c, weight=1)

    def _build_keybinds_tab(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        ptt_frame = ttk.LabelFrame(frm, text="Push-to-Talk", style="Card.TLabelframe")
        ptt_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for c in range(4):
            ptt_frame.columnconfigure(c, weight=1)

        row = 0
        ttk.Label(ptt_frame, text="PTT Mode:").grid(row=row, column=0, sticky="w", pady=(8, 2))
        ptt_mode_combo = ttk.Combobox(ptt_frame, textvariable=self.ptt_mode, values=["hold", "toggle"], width=10, state="readonly")
        ptt_mode_combo.grid(row=row, column=1, sticky="w", pady=(8, 2))

        self.ptt_state_label = ttk.Label(ptt_frame, text="PTT: RELEASED")
        self.ptt_state_label.grid(row=row, column=2, sticky="w", pady=(8, 2))

        row += 1
        self.ptt_combo_label = ttk.Label(ptt_frame, text=f"PTT Combo(s): {self._combo_list_to_display(self.ptt_combos)}")
        self.ptt_combo_label.grid(row=row, column=0, sticky="w", pady=2)

        self.bind_ptt_btn = ttk.Button(ptt_frame, text="Bind PTT Combo\u2026", command=lambda: self._start_combo_bind('ptt'))
        self.bind_ptt_btn.grid(row=row, column=1, sticky="w", pady=2)

        self.bind_hint = ttk.Label(ptt_frame, text="", foreground=self._palette.get("muted", "#666"))
        self.bind_hint.grid(row=row, column=2, columnspan=2, sticky="w", pady=2)
        self._muted_labels.append(self.bind_hint)

        chan_frame = ttk.LabelFrame(frm, text="Channel Keybinds", style="Card.TLabelframe")
        chan_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for c in range(6):
            chan_frame.columnconfigure(c, weight=1)

        row = 0
        self.next_combo_label = ttk.Label(chan_frame, text=f"Next: {self._combo_list_to_display(self.next_combos)}")
        self.next_combo_label.grid(row=row, column=0, sticky="w")
        self.prev_combo_label = ttk.Label(chan_frame, text=f"Prev: {self._combo_list_to_display(self.prev_combos)}")
        self.prev_combo_label.grid(row=row, column=1, sticky="w")
        self.bind_next_btn = ttk.Button(chan_frame, text="Bind Next Channel\u2026", command=lambda: self._start_combo_bind('next'))
        self.bind_next_btn.grid(row=row, column=2, sticky="e", pady=(2, 2))
        self.bind_prev_btn = ttk.Button(chan_frame, text="Bind Prev Channel\u2026", command=lambda: self._start_combo_bind('prev'))
        self.bind_prev_btn.grid(row=row, column=3, sticky="w", pady=(2, 2))

        row += 1
        self.vol_up_combo_label = ttk.Label(chan_frame, text=f"Vol +: {self._combo_list_to_display(self.vol_up_combos)}")
        self.vol_up_combo_label.grid(row=row, column=0, sticky="w")
        self.vol_down_combo_label = ttk.Label(chan_frame, text=f"Vol \u2212: {self._combo_list_to_display(self.vol_down_combos)}")
        self.vol_down_combo_label.grid(row=row, column=1, sticky="w")
        self.bind_vol_up_btn = ttk.Button(chan_frame, text="Bind Volume + \u2026", command=lambda: self._start_combo_bind('vol_up'))
        self.bind_vol_up_btn.grid(row=row, column=2, sticky="e", pady=(2, 2))
        self.bind_vol_down_btn = ttk.Button(chan_frame, text="Bind Volume \u2212 \u2026", command=lambda: self._start_combo_bind('vol_down'))
        self.bind_vol_down_btn.grid(row=row, column=3, sticky="w", pady=(2,  2))

        row += 1
        ttk.Separator(chan_frame, orient="horizontal").grid(row=row, column=0, columnspan=6, sticky="ew", pady=(8,6))
        row += 1
        ttk.Label(chan_frame, text="Direct channel keybinds:").grid(row=row, column=0, columnspan=6, sticky="w", pady=(0,2))
        row += 1
        self.chan_a_combo_label = ttk.Label(chan_frame, text=f"A: {self._combo_list_to_display(self.chan_a_combos)}")
        self.chan_a_combo_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0,2))
        self.bind_chan_a_btn = ttk.Button(chan_frame, text="Bind A \u2026", command=lambda: self._start_combo_bind('chan_a'))
        self.bind_chan_a_btn.grid(row=row, column=2, sticky="w", padx=(4,0), pady=(0,2))
        self.chan_b_combo_label = ttk.Label(chan_frame, text=f"B: {self._combo_list_to_display(self.chan_b_combos)}")
        self.chan_b_combo_label.grid(row=row, column=3, columnspan=2, sticky="w", pady=(0,2))
        self.bind_chan_b_btn = ttk.Button(chan_frame, text="Bind B \u2026", command=lambda: self._start_combo_bind('chan_b'))
        self.bind_chan_b_btn.grid(row=row, column=5, sticky="w", padx=(4,0), pady=(0,2))

        row += 1
        self.chan_c_combo_label = ttk.Label(chan_frame, text=f"C: {self._combo_list_to_display(self.chan_c_combos)}")
        self.chan_c_combo_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0,2))
        self.bind_chan_c_btn = ttk.Button(chan_frame, text="Bind C \u2026", command=lambda: self._start_combo_bind('chan_c'))
        self.bind_chan_c_btn.grid(row=row, column=2, sticky="w", padx=(4,0), pady=(0,2))
        self.chan_d_combo_label = ttk.Label(chan_frame, text=f"D: {self._combo_list_to_display(self.chan_d_combos)}")
        self.chan_d_combo_label.grid(row=row, column=3, columnspan=2, sticky="w", pady=(0,2))
        self.bind_chan_d_btn = ttk.Button(chan_frame, text="Bind D \u2026", command=lambda: self._start_combo_bind('chan_d'))
        self.bind_chan_d_btn.grid(row=row, column=5, sticky="w", padx=(4,0), pady=(0,2))

        input_frame = ttk.LabelFrame(frm, text="Input", style="Card.TLabelframe")
        input_frame.grid(row=2, column=0, sticky="ew")
        input_frame.columnconfigure(0, weight=1)

        joy_desc = ttk.Label(
            input_frame,
            text="Poll joystick/gamepad buttons for keybinds (PTT, channel hotkeys). Disable if a noisy device keeps firing inputs.",
            foreground=self._palette.get("muted", "#555"),
            justify="left",
            wraplength=520,
        )
        joy_desc.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._muted_labels.append(joy_desc)

        self.joy_toggle = ttk.Checkbutton(
            input_frame,
            text="Enable joystick/gamepad buttons for keybinds",
            variable=self.joystick_enabled,
            command=self._toggle_joystick_poller,
        )
        self.joy_toggle.grid(row=1, column=0, sticky="w")

    def _build_connection_tab(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        row = 0
        ttk.Label(frm, text="Server IP:").grid(row=row, column=0, sticky="w")
        self.ip_entry = ttk.Entry(frm, textvariable=self.server_ip, width=18)
        self.ip_entry.grid(row=row, column=1, sticky="w")

        ttk.Label(frm, text="Port:").grid(row=row, column=2, sticky="e", padx=(10, 0))
        self.port_entry = ttk.Entry(frm, textvariable=self.server_port, width=8)
        self.port_entry.grid(row=row, column=3, sticky="w")

        ttk.Label(frm, text="Network:").grid(row=row, column=4, sticky="e", padx=(10, 0))
        self.network_entry = ttk.Entry(frm, textvariable=self.network, width=12)
        self.network_entry.grid(row=row, column=5, sticky="w")

        row += 1
        connect_btn = ttk.Button(frm, text="Connect", command=self._on_connect_click)
        connect_btn.grid(row=row, column=1, sticky="w", pady=(6, 0))
        disconnect_btn = ttk.Button(frm, text="Disconnect", command=self._on_disconnect_click)
        disconnect_btn.grid(row=row, column=2, sticky="w", pady=(6, 0))

        # Steam ID / SSRC override
        row += 1
        ttk.Label(frm, text="Steam ID (SSRC):").grid(row=row, column=0, sticky="w")
        steam_entry = ttk.Entry(frm, textvariable=self.steam_ssrc_var, width=26)
        steam_entry.grid(row=row, column=1, columnspan=2, sticky="w")
        ttk.Button(frm, text="Update / Save", command=self._on_steam_ssrc_save).grid(
            row=row, column=3, sticky="w"
        )

        # Info text
        row += 1
        help_lbl = ttk.Label(
            frm,
            text=(
                "Enter the IP/Port of the UDP server machine.\n"
                "Optional: Steam ID (SteamID64) to override your SSRC.\n"
                "Changes apply on next Connect, or immediately when you\n"
                "press Update / Save while connected."
            ),
            foreground=self._palette.get("muted", "#555"),
            justify="left",
        )
        help_lbl.grid(row=row, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._muted_labels.append(help_lbl)

        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=row + 1, column=0, columnspan=4, sticky="ew", pady=(12, 6))

        for c in range(4):
            frm.columnconfigure(c, weight=1)

    def _build_settings_tab(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        ttk.Label(frm, text="Appearance").grid(row=0, column=0, sticky="w")
        desc = ttk.Label(
            frm,
            text="Switch between light and dark for the main client window. The radio overlay keeps its own look.",
            foreground=self._palette.get("muted", "#555"),
            justify="left",
            wraplength=520,
        )
        desc.grid(row=1, column=0, sticky="w", pady=(6, 4))
        self._muted_labels.append(desc)

        self.theme_toggle = ttk.Checkbutton(
            frm,
            text="Enable dark mode",
            variable=self.ui_theme,
            onvalue="dark",
            offvalue="light",
            command=self._on_theme_toggle,
        )
        self.theme_toggle.grid(row=2, column=0, sticky="w", pady=(6, 2))

        hint = ttk.Label(
            frm,
            text="Changes apply instantly and persist to config_user.json.",
            foreground=self._palette.get("muted", "#555"),
            justify="left",
        )
        hint.grid(row=3, column=0, sticky="w", pady=(2, 0))
        self._muted_labels.append(hint)

        row = 4
        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=row, column=0, sticky="ew", pady=(14, 10))
        row += 1

        ttk.Label(frm, text="Sound Effects").grid(row=row, column=0, sticky="w")
        row += 1
        sfx_desc = ttk.Label(
            frm,
            text="Adjust key up/down and channel switch sounds. 0% mutes, 50% is normal loudness, up to 200% boost.",
            foreground=self._palette.get("muted", "#555"),
            justify="left",
            wraplength=520,
        )
        sfx_desc.grid(row=row, column=0, sticky="w", pady=(6, 4))
        self._muted_labels.append(sfx_desc)
        row += 1

        self.sfx_value_label = ttk.Label(frm, text=self._format_sfx_volume_label())
        self.sfx_value_label.grid(row=row, column=0, sticky="w", pady=(0, 4))
        row += 1

        sfx_scale = tk.Scale(
            frm,
            from_=0,
            to=100,
            orient="horizontal",
            variable=self.sfx_slider_var,
            resolution=1,
            showvalue=True,
            length=320,
            command=lambda _v=None: self._apply_sfx_volume(_v, save=False),
        )
        sfx_scale.grid(row=row, column=0, sticky="w")
        sfx_scale.bind("<ButtonRelease-1>", lambda _e=None: self._apply_sfx_volume(None, save=True))
        sfx_scale.bind("<FocusOut>", lambda _e=None: self._apply_sfx_volume(None, save=True))
        self._scales.append(sfx_scale)

        # Ensure label text matches the current slider position from config
        self._apply_sfx_volume(save=False)

    def _toggle_joystick_poller(self):
        enabled = bool(self.joystick_enabled.get())
        try:
            if hasattr(self, "global_keys") and self.global_keys:
                self.global_keys.set_gamepad_polling(enabled)
        except Exception:
            pass
        try:
            self._save_user_config_all()
        except Exception:
            pass

    def _sfx_gain_from_slider(self, slider_val: float | None) -> float:
        try:
            v = float(slider_val if slider_val is not None else self.sfx_slider_var.get())
        except Exception:
            v = 50.0
        v = max(0.0, min(100.0, v))
        if v <= 50.0:
            return v / 50.0
        return 1.0 + ((v - 50.0) / 50.0)

    def _sfx_slider_from_gain(self, gain: float) -> float:
        try:
            g = float(gain)
        except Exception:
            g = 1.0
        g = max(0.0, min(2.0, g))
        if g <= 1.0:
            return g * 50.0
        return 50.0 + (g - 1.0) * 50.0

    def _format_sfx_volume_label(self, slider_val: float | None = None, gain: float | None = None) -> str:
        if slider_val is None:
            try:
                slider_val = float(self.sfx_slider_var.get())
            except Exception:
                slider_val = 50.0
        if gain is None:
            gain = self._sfx_gain_from_slider(slider_val)
        pct = int(round(slider_val))
        if gain <= 0:
            desc = "Muted"
        elif abs(gain - 1.0) < 1e-3:
            desc = "Normal"
        elif gain < 1.0:
            desc = f"{int(round(gain * 100))}% of normal"
        else:
            desc = f"Boost {gain:.2f}x"
        return f"Effects volume: {pct}% ({desc})"

    def _apply_sfx_volume(self, slider_val=None, save: bool = False):
        try:
            val = float(slider_val if slider_val is not None else self.sfx_slider_var.get())
        except Exception:
            val = 50.0
        val = max(0.0, min(100.0, val))
        try:
            self.sfx_slider_var.set(val)
        except Exception:
            pass
        gain = self._sfx_gain_from_slider(val)
        try:
            self.sfx_gain.set(gain)
        except Exception:
            pass
        try:
            if hasattr(self, "sfx_value_label"):
                self.sfx_value_label.configure(text=self._format_sfx_volume_label(val, gain))
        except Exception:
            pass
        try:
            if hasattr(self, "sounds"):
                self.sounds.set_gain(gain)
        except Exception:
            pass
        if save:
            try:
                self._save_user_config_all()
            except Exception:
                pass

    def _build_debug_tab(self, parent):
        frm = ttk.Frame(parent, padding=12)
        # --- Mic Input VU ---
        self._vu_text = tk.StringVar(value="Mic: 0%")
        row = 2
        ttk.Label(frm, text="Mic Input Level").grid(row=row, column=0, sticky="w", pady=(12,2))
        self._vu_bar = ttk.Progressbar(frm, orient="horizontal", mode="determinate", maximum=100, length=260)
        self._vu_bar.grid(row=row, column=1, sticky="ew", padx=(10,0))
        try:
            frm.columnconfigure(1, weight=1)
        except Exception:
            pass
        ttk.Label(frm, textvariable=self._vu_text).grid(row=row+1, column=0, columnspan=2, sticky="w")
        self._vu_btn = ttk.Button(frm, text="Start Mic Test", command=self._toggle_vu_test)
        self._vu_btn.grid(row=row, column=2, padx=(10,0), sticky="w")
        frm.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1); parent.rowconfigure(0, weight=1)

        desc = ("Active Channel Debug simulates remote talkers.\n"
                "Put test1..test5 as .ogg/.wav (overlap) or .mp3 in:\n"
                "  C:\\Users\\lscott\\Desktop\\SE-Radio-Client-v0.1\\Audio\\Test\n"
                "or project-relative: ..\\Audio\\Test\n"
                "It will randomly key channels A–D (sometimes multiple), play a random test file, and drive the overlay’s “Active:” line.")
        ttk.Label(frm, text=desc, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")

        self.debug_btn = ttk.Button(frm, text="Start Active Channel Debug", command=self._toggle_debug_rx)
        self.debug_btn.grid(row=1, column=0, sticky="w", pady=(10,0))

        self.debug_status = tk.StringVar(value="Status: idle")
        self.debug_status_label = ttk.Label(frm, textvariable=self.debug_status, foreground=self._palette.get("muted", "#555"))
        self.debug_status_label.grid(row=1, column=1, sticky="w", padx=(10,0))
        self._muted_labels.append(self.debug_status_label)

        # Loopback toggle (server round-trip test)
        ttk.Label(frm, text="Radio Loopback (server round-trip)").grid(row=4, column=0, sticky="w", pady=(14,2))
        self.loopback_chk = ttk.Checkbutton(frm, text="Send my TX to server and back to me", variable=self.loopback_enabled, command=self._apply_loopback_setting)
        self.loopback_chk.grid(row=4, column=1, sticky="w", padx=(4,0))

    # ---------- Theme ----------
    def _get_palette(self, mode: str):
        if str(mode).lower() == "dark":
            return {
                "bg": "#0b1220",
                "surface": "#111a2e",
                "fg": "#e5e9f0",
                "muted": "#94a3b8",
                "border": "#1f2a3d",
                "tab_bg": "#0f172a",
                "tab_active_bg": "#15233b",
                "button_bg": "#182640",
                "button_active_bg": "#1f3352",
                "entry_bg": "#0f1b2f",
                "tree_bg": "#0f172a",
                "select_bg": "#1f3f66",
                "select_fg": "#e5e9f0",
                "accent": "#7bd0ff",
                "good": "#4ade80",
                "bad": "#f47272",
                "progress": "#7bd0ff",
                "surface_border": "#1f2a3d",
            }
        return {
            "bg": "#edf1f7",
            "surface": "#f8fafc",
            "fg": "#0f172a",
            "muted": "#64748b",
            "border": "#d5dbe6",
            "tab_bg": "#e7ebf3",
            "tab_active_bg": "#ffffff",
            "button_bg": "#e4e9f2",
            "button_active_bg": "#d8e0ed",
            "entry_bg": "#ffffff",
            "tree_bg": "#ffffff",
            "select_bg": "#c2dbff",
            "select_fg": "#0f172a",
            "accent": "#3b82f6",
            "good": "#16a34a",
            "bad": "#dc2626",
            "progress": "#3b82f6",
            "surface_border": "#d5dbe6",
        }

    def _colorref_from_hex(self, value: str | None):
        """Convert #RRGGBB -> COLORREF (0x00BBGGRR) for DWM attributes."""
        try:
            if not value or not isinstance(value, str):
                return None
            value = value.lstrip("#")
            if len(value) != 6:
                return None
            r = int(value[0:2], 16)
            g = int(value[2:4], 16)
            b = int(value[4:6], 16)
            return (b << 16) | (g << 8) | r
        except Exception:
            return None

    def _apply_windows_titlebar(self, palette: dict, mode: str):
        """Keep the Windows title bar aligned with the current theme."""
        if sys.platform != "win32":
            return
        try:
            hwnd = self.root.winfo_id()
        except Exception:
            return

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Pre-2004 builds
        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36

        def _set_attr(h, attr: int, val, size: int | None = None):
            try:
                size = size or ctypes.sizeof(val)
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(h, attr, ctypes.byref(val), size)
                return res == 0
            except Exception:
                return False

        # Tk sometimes nests child windows; try both the window and its root ancestor.
        targets = [hwnd]
        try:
            ga_root = ctypes.windll.user32.GetAncestor(wintypes.HWND(hwnd), ctypes.c_uint(2))  # GA_ROOT
            if ga_root and ga_root not in targets:
                targets.append(ga_root)
        except Exception:
            pass

        dark_flag = ctypes.c_int(1 if mode == "dark" else 0)
        bg_color = self._colorref_from_hex(palette.get("bg"))
        text_color = self._colorref_from_hex(palette.get("fg"))
        reset = ctypes.c_int(-1)

        for h in targets:
            _set_attr(h, DWMWA_USE_IMMERSIVE_DARK_MODE, dark_flag)
            _set_attr(h, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, dark_flag)

            if bg_color is not None and mode == "dark":
                _set_attr(h, DWMWA_CAPTION_COLOR, ctypes.c_int(bg_color))
            else:
                _set_attr(h, DWMWA_CAPTION_COLOR, reset)

            if text_color is not None and mode == "dark":
                _set_attr(h, DWMWA_TEXT_COLOR, ctypes.c_int(text_color))
            else:
                _set_attr(h, DWMWA_TEXT_COLOR, reset)

            # Fallback for older Windows 10 builds that respond to a theme hint.
            try:
                ctypes.windll.uxtheme.SetWindowTheme(wintypes.HWND(h), "DarkMode_Explorer" if mode == "dark" else None, None)
            except Exception:
                pass

    def _apply_theme(self, mode: str | None = None):
        mode = mode or self.ui_theme.get()
        mode = str(mode).lower()
        if mode not in ("light", "dark"):
            mode = "light"
            self.ui_theme.set("light")
        self._palette = self._get_palette(mode)
        try:
            if mode == "dark":
                self._style.theme_use("clam")
            else:
                self._style.theme_use(self._base_theme)
        except Exception:
            try:
                self._style.theme_use("clam" if mode == "dark" else "default")
            except Exception:
                pass

        p = self._palette
        self._apply_windows_titlebar(p, mode)
        s = self._style
        try:
            self.root.configure(bg=p["bg"])
        except Exception:
            pass

        try:
            s.configure(".", font=self._font_body)
        except Exception:
            pass

        try:
            s.configure("TFrame", background=p.get("surface", p["bg"]))
            s.configure("TLabel", background=p.get("surface", p["bg"]), foreground=p["fg"], font=self._font_body)
        except Exception:
            pass
        try:
            s.configure("TNotebook", background=p["bg"], borderwidth=0, tabmargins=(8,6,8,0))
        except Exception:
            pass
        try:
            s.configure("TNotebook.Tab", background=p["tab_bg"], foreground=p["muted"], padding=(14,8), font=self._font_heading)
            s.map(
                "TNotebook.Tab",
                background=[("selected", p["tab_active_bg"]), ("active", p["tab_active_bg"]), ("hover", p["tab_active_bg"])],
                foreground=[("selected", p["fg"]), ("active", p["fg"]), ("hover", p["fg"])],
            )
        except Exception:
            pass
        try:
            s.configure("TButton", background=p["button_bg"], foreground=p["fg"], padding=(12, 8), font=self._font_heading, borderwidth=0)
            s.map(
                "TButton",
                background=[("active", p["button_active_bg"]), ("pressed", p["button_active_bg"])],
                foreground=[("disabled", p["muted"])],
            )
        except Exception:
            pass
        try:
            s.configure("TCheckbutton", background=p.get("surface", p["bg"]), foreground=p["fg"], padding=4)
            s.map("TCheckbutton", background=[("active", p["tab_bg"])])
        except Exception:
            pass
        try:
            s.configure("TEntry", foreground=p["fg"], fieldbackground=p["entry_bg"], insertcolor=p["fg"], padding=6)
            s.configure("TCombobox", foreground=p["fg"], fieldbackground=p["entry_bg"], background=p["entry_bg"], padding=6)
            s.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"]), ("!disabled", p["entry_bg"])])
        except Exception:
            pass
        try:
            s.configure("TScale", background=p.get("surface", p["bg"]), troughcolor=p["border"])
        except Exception:
            pass
        try:
            # Labelframe (used for channel block) needs explicit background to avoid white box in dark mode
            s.configure("TLabelFrame", background=p.get("surface", p["bg"]), foreground=p["fg"])
            s.configure("TLabelframe", background=p.get("surface", p["bg"]), foreground=p["fg"], bordercolor=p.get("surface_border", p["border"]))
            s.configure("TLabelframe.Label", background=p.get("surface", p["bg"]), foreground=p["fg"], font=self._font_heading)
            s.configure("Card.TLabelframe", background=p.get("surface", p["bg"]), foreground=p["fg"], bordercolor=p.get("surface_border", p["border"]))
            s.configure("Card.TLabelframe.Label", background=p.get("surface", p["bg"]), foreground=p["fg"], font=self._font_heading)
        except Exception:
            pass
        try:
            s.configure("Treeview", background=p["tree_bg"], foreground=p["fg"], fieldbackground=p["tree_bg"],
                        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
            s.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
            s.configure("Treeview.Heading", background=p["tab_bg"], foreground=p["fg"])
        except Exception:
            pass
        try:
            s.configure("Horizontal.TProgressbar", background=p["progress"], troughcolor=p["border"])
        except Exception:
            pass

        for lbl in list(getattr(self, "_muted_labels", [])):
            try:
                lbl.configure(foreground=p["muted"])
            except Exception:
                pass

        for sc in list(getattr(self, "_scales", [])):
            try:
                sc.configure(
                    bg=p.get("surface", p["bg"]),
                    fg=p["fg"],
                    troughcolor=p["border"],
                    highlightbackground=p.get("surface", p["bg"]),
                    highlightcolor=p.get("surface", p["bg"]),
                    activebackground=p["accent"],
                )
            except Exception:
                pass

        try:
            if hasattr(self, "status"):
                self.status.configure(foreground=p["fg"])
        except Exception:
            pass

        try:
            if hasattr(self, "nb"):
                self.nb.configure(style="TNotebook")
        except Exception:
            pass

        self._update_connection_indicator()

    def _on_theme_toggle(self):
        self._apply_theme(self.ui_theme.get())
        try:
            self._save_user_config_all()
        except Exception:
            pass

    # ---------- Input debug viewer ----------
    def _open_input_debugger(self):
        if self._input_debugger and self._input_debugger.is_open():
            self._input_debugger.focus()
            return
        try:
            with self._input_debug_lock:
                self._input_debug_buffer.clear()
        except Exception:
            pass
        self._input_debug_enabled = True
        self._input_debugger = InputDebugWindow(self, on_close=self._on_input_debugger_closed, on_ignore=self._ignore_tokens_from_debugger)
        try:
            self._input_debugger.open()
        except Exception:
            self._on_input_debugger_closed()
            return
        try:
            self._flush_input_debug_events()
        except Exception:
            pass
        try:
            self._debug_snapshot_current_inputs(origin_label="Snapshot")
        except Exception:
            pass

    def _on_input_debugger_closed(self):
        self._input_debug_enabled = False
        try:
            with self._input_debug_lock:
                self._input_debug_buffer.clear()
        except Exception:
            pass
        self._input_debugger = None

    def _enqueue_input_debug_event(self, origin, token, action, detail="", pressed_snapshot=None):
        if not self._input_debug_enabled:
            return
        try:
            ts = time.strftime("%H:%M:%S")
        except Exception:
            ts = ""
        evt = {
            "time": ts,
            "origin": origin or "",
            "token": token or "",
            "action": action or "",
            "detail": detail or "",
            "pressed": tuple(pressed_snapshot) if pressed_snapshot else tuple(),
        }
        try:
            with self._input_debug_lock:
                self._input_debug_buffer.append(evt)
        except Exception:
            pass
        try:
            self.root.after(0, self._flush_input_debug_events)
        except Exception:
            pass

    def _flush_input_debug_events(self):
        if not (self._input_debug_enabled and self._input_debugger and self._input_debugger.is_open()):
            return
        try:
            with self._input_debug_lock:
                batch = list(self._input_debug_buffer)
                self._input_debug_buffer.clear()
        except Exception:
            batch = []
        if not batch:
            return
        try:
            pressed = tuple(sorted(self._current_tokens()))
        except Exception:
            pressed = tuple()
        try:
            self._input_debugger.record_events(batch, pressed)
        except Exception:
            pass

    def _debug_snapshot_current_inputs(self, origin_label="Snapshot"):
        """Emit synthetic debug events for inputs that are already held when the debugger opens."""
        if not self._input_debug_enabled:
            return
        try:
            tokens = tuple(sorted(self._current_tokens()))
        except Exception:
            tokens = tuple()
        if not tokens:
            return
        for tok in tokens:
            try:
                self._enqueue_input_debug_event(origin_label, tok, "held", detail="already pressed", pressed_snapshot=tokens)
            except Exception:
                pass

    def _is_token_ignored(self, token: str) -> bool:
        try:
            norm = self._normalize_token(token)
        except Exception:
            norm = str(token or "")
        return bool(norm and norm in self._ignored_tokens)

    def _ignore_tokens_from_debugger(self, tokens, unignore=None):
        added = []
        removed = []
        try:
            for t in tokens or []:
                norm = self._normalize_token(t)
                if norm and norm not in self._ignored_tokens:
                    self._ignored_tokens.add(norm)
                    added.append(norm)
            for t in unignore or []:
                norm = self._normalize_token(t)
                if norm and norm in self._ignored_tokens:
                    self._ignored_tokens.discard(norm)
                    removed.append(norm)
        except Exception:
            pass
        try:
            # Persist
            self._save_user_config_all()
        except Exception:
            pass

    def _request_input_refresh(self, source: str = ""):
        """Coalesce PTT/channel recomputes and keep Tk work on the main thread."""
        try:
            if threading.current_thread() is threading.main_thread():
                self._update_ptt_and_channels(source=source)
                return
        except Exception:
            pass
        try:
            with self._input_refresh_lock:
                if not self._input_refresh_event.is_set() or source:
                    self._input_refresh_last_source = source or self._input_refresh_last_source
                self._input_refresh_event.set()
        except Exception:
            pass

    def _drain_input_refresh(self):
        try:
            if self._input_refresh_event.is_set():
                self._input_refresh_event.clear()
                try:
                    with self._input_refresh_lock:
                        src = self._input_refresh_last_source
                        self._input_refresh_last_source = ""
                except Exception:
                    src = ""
                self._update_ptt_and_channels(source=src)
        except Exception:
            pass
        try:
            self.root.after(self._input_refresh_poll_ms, self._drain_input_refresh)
        except Exception:
            pass

    # ---------- Status & timers ----------
    def _update_connection_indicator(self):
        if self.connected.get():
            self.conn_indicator.config(text="● Connected", foreground=self._palette.get("good", "#1a1"))
        else:
            self.conn_indicator.config(text="○ Disconnected", foreground=self._palette.get("bad", "#b11"))

    def _tick_ui(self):
        self._maybe_finalize_bind_poll()
        self._update_active_label(); self._update_connection_indicator(); self._update_audible_hint()
        self.root.after(100, self._tick_ui)

    def _tick_rx_expire(self):
        # Flush any pending RX lamp updates from network thread
        try:
            with self._rx_lamp_lock:
                pending = list(self._rx_lamp_queue)
                self._rx_lamp_queue.clear()
            for idx in pending:
                try:
                    self.set_rx_channel_state(int(idx), True)
                except Exception:
                    pass
        except Exception:
            pass

        now_ms = int(time.time()*1000)
        changed = False
        for i in range(4):
            if i in self.rx_active and now_ms >= self.rx_last_until[i]:
                self.rx_active.discard(i); changed = True
        if changed:
            self.status_text.set(self._active_rx_text())
        self.root.after(100, self._tick_rx_expire)

    # ---------- RX helpers (called by network or debug) ----------
    def set_rx_channel_state(self, idx: int, is_active: bool):
        idx = max(0, min(3, int(idx)))
        now_ms = int(time.time()*1000)
        if is_active:
            self.rx_active.add(idx)
            self.rx_last_until[idx] = now_ms + self.rx_hang_ms
            if self._debug_audio.can_play():
                self._debug_audio.play_on_channel(idx)
        else:
            self.rx_last_until[idx] = now_ms + self.rx_hang_ms
        self.status_text.set(self._active_rx_text())

    def _active_rx_text(self):
        order = [0,1,2,3]
        names = "ABCD"
        freqs = [self.chan_vars[0].get(), self.chan_vars[1].get(), self.chan_vars[2].get(), self.chan_d_var.get()]
        lst = []
        for i in order:
            if i in self.rx_active:
                lst.append(f"Channel {names[i]} #{freqs[i]}")
        return "Active: " + (", ".join(lst) if lst else "None")

    def get_active_rx_channels(self):
        order = [0,1,2,3]
        freqs = [self.chan_vars[0].get(), self.chan_vars[1].get(), self.chan_vars[2].get(), self.chan_d_var.get()]
        result = []
        for i in order:
            if i in self.rx_active:
                result.append((i, freqs[i]))
        return result

    # ---------- Channel logic ----------
    def _active_chan_label(self):
        idx = self.active_chan.get()
        names = ["A","B","C","D"]
        freqs = [self.chan_vars[0].get(), self.chan_vars[1].get(), self.chan_vars[2].get(), self.chan_d_var.get()]
        vol = [int(self.chan_vol_vars[0].get()), int(self.chan_vol_vars[1].get()), int(self.chan_vol_vars[2].get()), int(self.chan_d_vol_var.get())]
        fmt = freqs[idx] or "___._"
        return f"Active: {names[idx]} ({fmt} MHz, Vol {vol[idx]}%)"

    def _activate_channel(self, idx: int):
        """Set the active channel (0-3) and run switch side effects."""
        try:
            idx = int(idx)
        except Exception:
            return
        idx = max(0, min(3, idx))
        if self.active_chan.get() == idx:
            return
        self.active_chan.set(idx)
        self._after_channel_change()

    def _cycle_next(self):
        self._activate_channel((self.active_chan.get()+1)%4)

    def _cycle_prev(self):
        self._activate_channel((self.active_chan.get()-1)%4)

    def _bump_active_volume(self, delta: int):
        idx = int(self.active_chan.get())
        if idx < 3:
            v = int(self.chan_vol_vars[idx].get())
            v = max(0, min(100, v + delta)); v = (v // 10) * 10
            self.chan_vol_vars[idx].set(v)
        else:
            v = int(self.chan_d_vol_var.get())
            v = max(30, min(100, v + delta)); v = (v // 10) * 10
            self.chan_d_vol_var.set(v)
        self._save_user_config_all()
        self._update_audible_hint()
        self._update_active_label()
        try: self._show_channel_osd()
        except Exception: pass
        try: self.sounds.play_volume()
        except Exception: pass

    def _after_channel_change(self):
        try:
            self._send_chan_update()
        except Exception:
            pass
        self._update_active_label()
        try: self._show_channel_osd()
        except Exception: pass
        try: self.sounds.play_switch()
        except Exception: pass
        self.status_text.set(self._active_chan_label())
        self._save_user_config_all()
        self._update_audible_hint()
        self._notify_server()

    # --------------- Debounced frequency notifier ---------------
    def _on_freq_var_changed(self):
        """Debounce edits and send join once user pauses typing."""
        if not self.connected.get():
            return
        if getattr(self, "_freq_debounce_after_id", None):
            try: self.root.after_cancel(self._freq_debounce_after_id)
            except Exception: pass
            self._freq_debounce_after_id = None
        try:
            self._freq_debounce_after_id = self.root.after(self._freq_debounce_ms, self._notify_server)
        except Exception:
            pass

    # ----------------- Debug mic level hook -----------------
    def on_mic_level(self, level: float):
        try:
            lvl = max(0.0, min(1.0, float(level)))
        except Exception:
            return
        def _upd():
            try:
                if hasattr(self, '_vu_bar') and self._vu_bar:
                    self._vu_bar['value'] = int(lvl*100)
                if hasattr(self, '_vu_text') and self._vu_text:
                    self._vu_text.set(f"Mic: {int(lvl*100)}%")
            except Exception:
                pass
        try:
            self.root.after(0, _upd)
        except Exception:
            _upd()

    # ----------------- Debug Mic Test control -----------------
    def _toggle_vu_test(self):
        """Start/stop a direct mic capture test (independent of AudioEngine)."""
        try:
            if not hasattr(self, '_vu_testing'):
                self._vu_testing = tk.BooleanVar(value=False)
            new_state = not bool(self._vu_testing.get())
            self._vu_testing.set(new_state)
            # Update button label
            try:
                if hasattr(self, '_vu_btn') and self._vu_btn:
                    self._vu_btn.configure(text=("Stop Mic Test" if new_state else "Start Mic Test"))
            except Exception:
                pass
            # Stop existing stream if any
            if getattr(self, '_vu_stream', None) is not None:
                try:
                    self._vu_stream.stop(); self._vu_stream.close()
                except Exception:
                    pass
                self._vu_stream = None
            if not new_state:
                return
            # Require sounddevice/numpy
            if sd is None or _np is None:
                try:
                    self.status_text.set("Install 'sounddevice' and 'numpy' for Mic Test")
                except Exception:
                    pass
                return
            # Open an input stream (prefer 16k mono; fallback to defaults)
            def _cb(indata, frames, time_info, status):
                try:
                    ch0 = indata[:,0] if (hasattr(indata, 'ndim') and indata.ndim > 1) else indata
                    lvl = float(_np.sqrt(_np.mean(ch0**2))) if frames > 0 else 0.0
                    self.on_mic_level(lvl)
                except Exception:
                    pass
            try:
                self._vu_stream = sd.InputStream(samplerate=16000, channels=1, dtype='float32', callback=_cb)
            except Exception:
                self._vu_stream = sd.InputStream(callback=_cb)
            self._vu_stream.start()
        except Exception:
            pass

    # ----------------- Notify server helpers -----------------
    
    def _current_scan_state(self):
        """Return (scan_enabled, scan_channels[4]) normalized for channels A-D."""
        scan = False
        scan_channels = [False, False, False, False]
        try:
            flags = [v.get() for v in getattr(self, "scan_vars", [])]
            if hasattr(self, "scan_d_var"):
                flags.append(self.scan_d_var.get())
            scan_channels = [bool(x) for x in flags][:4]
            if len(scan_channels) < 4:
                scan_channels = (scan_channels + [False, False, False, False])[:4]
            scan = any(scan_channels)
        except Exception:
            try:
                scan = bool(getattr(self, "scan_enabled", False))
                scan_channels = [bool(scan)] * 4
            except Exception:
                pass
        return bool(scan), scan_channels

    def _notify_server(self):
        """
        Notify the UDP server about current channel selection / scan state.
        Uses udp_client.update_channels() with JSON the server understands.
        """
        # Also keep any local/overlay listeners updated
        try:
            self._send_chan_update()
        except Exception:
            pass

        if not hasattr(self, '_udp') or not self._udp:
            return

        # Collect frequencies
        try:
            freqs = [
                float(self.chan_vars[0].get()),
                float(self.chan_vars[1].get()),
                float(self.chan_vars[2].get()),
                float(self.chan_d_var.get()),
            ]
        except Exception:
            try:
                freqs = [float(x) for x in getattr(self, "chan_freqs", [0.0, 0.0, 0.0, 0.0])]
            except Exception:
                freqs = [0.0, 0.0, 0.0, 0.0]

        # Active channel index
        try:
            active_idx = int(self.active_chan.get())
        except Exception:
            active_idx = int(getattr(self, "active_channel_idx", 0) or 0)
        active_idx = max(0, min(3, active_idx))

        # Scan enabled? Track both overall scan and per-channel scan flags.
        scan, scan_channels = self._current_scan_state()

        state = {
            "active_channel": active_idx,
            "freqs": freqs,
            "scan": bool(scan),
            "scan_channels": scan_channels,
        }
        try:
            self._udp.update_channels(state)
        except Exception:
            pass

    def _update_active_label(self):
        if hasattr(self, "active_label"):
            self.active_label.config(text=self._active_chan_label())

    def _on_scan_changed(self):
        self._save_user_config_all()
        self._update_audible_hint()
        self._notify_server()

    def _update_audible_hint(self):
        names = ["A","B","C","D"]
        vols = [self.chan_vol_vars[0].get(), self.chan_vol_vars[1].get(), self.chan_vol_vars[2].get(), self.chan_d_vol_var.get()]
        audible = []
        for i in range(3):
            if self.channel_is_audible(i):
                audible.append(f"{names[i]}({int(vols[i])}%)")
        if self.channel_is_audible(3):
            audible.append(f"D({int(vols[3])}%)")
        fmt_list = ", ".join(audible) if audible else "None"
        self.audible_hint.config(text=f"Audible channels now: {fmt_list}")

    # ---------- Combo utilities ----------
    def _normalize_token(self, token: str) -> str:
        """Canonicalize key tokens so capture/load/global listeners agree."""
        t = (str(token or "").strip())
        if not t:
            return ""
        key = t.replace("_", "").replace(" ", "")
        key_up = key.upper()
        alias = {
            "SHIFT": "Shift",
            "CTRL": "Ctrl", "CONTROL": "Ctrl",
            "ALT": "Alt", "OPTION": "Alt", "META": "Alt",
            "WIN": "Win", "WINDOWS": "Win", "SUPER": "Win", "CMD": "Win",
            "CAPSLOCK": "CapsLock",
            "NUMLOCK": "NumLock",
            "SCROLLLOCK": "ScrollLock",
        }
        if key_up in alias:
            return alias[key_up]
        if key_up.startswith("JOYBTN") and key_up[6:].isdigit():
            return f"JoyBtn{int(key_up[6:])}"
        if key_up.startswith("JOYHAT"):
            rest = key_up[6:]
            num_part = ""
            dir_part = rest
            while dir_part and dir_part[0].isdigit():
                num_part += dir_part[0]
                dir_part = dir_part[1:]
            dir_norm = {"UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right"}
            if dir_part:
                d = dir_norm.get(dir_part.upper())
                if d:
                    prefix = f"JoyHat{int(num_part)}" if num_part else "JoyHat"
                    return f"{prefix}{d}"
        if key_up.startswith("F") and key_up[1:].isdigit():
            return f"F{int(key_up[1:])}"
        if len(key) == 1:
            return key.upper()
        return t

    def _normalize_key(self, keysym):
        mod_norm = {"Shift_L":"Shift","Shift_R":"Shift","Control_L":"Ctrl","Control_R":"Ctrl","Alt_L":"Alt","Alt_R":"Alt","Meta_L":"Alt","Meta_R":"Alt"}
        if keysym in mod_norm: return mod_norm[keysym]
        return self._normalize_token(keysym)

    def _combo_to_string(self, tokens):
        mods_order = ["Shift","Ctrl","Alt"]
        mods = [m for m in mods_order if m in tokens]
        mains = sorted([t for t in tokens if t not in mods_order])
        return "+".join(mods+mains) if mods or mains else ""

    def _string_to_combo(self, s):
        s=(s or "").strip()
        if not s:
            return frozenset()
        tokens = [self._normalize_token(p) for p in s.split("+")]
        return frozenset(t for t in tokens if t)

    def _normalize_combo_list(self, combos):
        """Normalize arbitrary combo inputs into a unique, trimmed list."""
        normalized = []
        if combos is None:
            return normalized
        # Allow single combo represented as str/frozenset/set
        if isinstance(combos, (str, set, frozenset)):
            combos = [combos]
        for combo in combos:
            combo_tokens = None
            if isinstance(combo, (set, frozenset)):
                combo_tokens = frozenset(self._normalize_token(t) for t in combo if t)
            elif isinstance(combo, str):
                combo_tokens = self._string_to_combo(combo)
            elif isinstance(combo, (list, tuple)) and combo and all(isinstance(t, str) for t in combo):
                combo_tokens = frozenset(self._normalize_token(t) for t in combo if t)
            if combo_tokens is None or not combo_tokens:
                continue
            if combo_tokens not in normalized:
                normalized.append(combo_tokens)
            if len(normalized) >= 3:
                break
        return normalized

    def _combo_list_to_display(self, combos):
        """Human label for a list of combos (e.g., 'Ctrl+X | F7')."""
        normalized = self._normalize_combo_list(combos)
        if not normalized:
            return "None"
        return " | ".join(self._combo_to_string(c) or "None" for c in normalized)

    def _serialize_combos(self, combos):
        """Convert a combo list into a JSON-safe list of strings."""
        return [self._combo_to_string(c) for c in self._normalize_combo_list(combos) if c]

    def _global_pressed_tokens(self) -> frozenset:
        """Thread-safe snapshot of global tokens captured by the keyboard hook."""
        try:
            if hasattr(self, "global_keys") and self.global_keys:
                return frozenset(self.global_keys.snapshot_pressed_normal())
        except Exception:
            pass
        try:
            return frozenset(self._pressed_global)
        except Exception:
            return frozenset()

    def _current_tokens(self):
        tokens = set(self._normalize_key(ks) for ks in self._pressed)
        try:
            tokens |= set(self._global_pressed_tokens())
        except Exception:
            pass
        return frozenset(t for t in tokens if t)

    def _combo_is_active(self, combo, tokens=None):
        if tokens is None:
            tokens = (self._global_pressed_tokens() if have_pynput() and not self._waiting_bind else self._current_tokens())
        for cand in self._normalize_combo_list(combo):
            if cand and cand.issubset(tokens):
                return True
        return False

    # ---------- RX jitter buffer ----------
    def _enqueue_rx_frame(self, buf, rate, chan_idx=None, src_ssrc=None):
        """Normalize incoming RX frames to AUDIO_RATE/AUDIO_BLOCK and enqueue with metadata for mixing."""
        try:
            import numpy as np, time as _time
            arr = np.asarray(buf, dtype=np.float32).reshape(-1)
            if rate and rate > 0 and rate != AUDIO_RATE and len(arr) > 0:
                n_target = max(1, int(round(len(arr) * (AUDIO_RATE / float(rate)))))
                x_old = np.linspace(0.0, 1.0, num=len(arr), endpoint=False, dtype=np.float32)
                x_new = np.linspace(0.0, 1.0, num=n_target, endpoint=False, dtype=np.float32)
                arr = np.interp(x_new, x_old, arr).astype(np.float32)
            if len(arr) < AUDIO_BLOCK:
                arr = np.pad(arr, (0, AUDIO_BLOCK - len(arr)), mode="constant")
            elif len(arr) > AUDIO_BLOCK:
                arr = arr[:AUDIO_BLOCK]

            ts = _time.time()
            with self._rx_lock:
                # Drop very stale backlog so we don't play half-second-old bursts after unkeying.
                STALE_SEC = 0.7
                while self._rx_queue and ts - float(self._rx_queue[0].get("ts", ts)) > STALE_SEC:
                    self._rx_queue.popleft()
                self._rx_queue.append({
                    "buf": arr,
                    "chan_idx": chan_idx,
                    "ssrc": src_ssrc,
                    "ts": ts,
                })
            try:
                now_t = ts
                self.last_rx_ts = now_t
                self.rx_active_recent_ts = now_t
            except Exception:
                pass
        except Exception:
            pass

    def _dequeue_rx_frame(self):
        try:
            import numpy as np, time as _time
            with self._rx_lock:
                if not self._rx_queue:
                    return None

                now = _time.time()
                STALE_SEC = 0.6
                TARGET_DEPTH = 6    # ~60 ms playout buffer before starting (reduce lag/robot effect)
                MAX_DEPTH = 80
                MAX_BATCH = 6       # max frames to mix at once
                JOIN_WINDOW = 0.050  # widen window so overlapping talkers get layered instead of time-sliced

                while self._rx_queue and now - float(self._rx_queue[0].get("ts", now)) > STALE_SEC:
                    self._rx_queue.popleft()
                while len(self._rx_queue) > MAX_DEPTH:
                    self._rx_queue.popleft()

                if not self._rx_started:
                    if len(self._rx_queue) < TARGET_DEPTH:
                        if self._rx_last_frame is not None and self._rx_last_repeat < 2:
                            self._rx_last_repeat += 1
                            # Light decay to avoid steady buzz
                            return (self._rx_last_frame * (0.6 ** self._rx_last_repeat)).copy()
                        return None
                    self._rx_started = True
                else:
                    if len(self._rx_queue) < 2:
                        # Not enough to play a fresh frame; reuse last good frame briefly, then silence.
                        if self._rx_last_frame is not None and self._rx_last_repeat < 3:
                            self._rx_last_repeat += 1
                            return (self._rx_last_frame * (0.5 ** self._rx_last_repeat)).copy()
                        # Drop the cached frame so we do not loop a stale tail forever during silence.
                        self._rx_last_repeat = 0
                        self._rx_last_frame = None
                        return np.zeros(AUDIO_BLOCK, dtype=np.float32)

                first = self._rx_queue.popleft()
                batch = [first]
                first_ts = float(first.get("ts", now))
                first_ssrc = first.get("ssrc")
                seen_ssrc = {first_ssrc if first_ssrc is not None else id(first)}
                defer_same = []
                while self._rx_queue and len(batch) < MAX_BATCH:
                    nxt = self._rx_queue[0]
                    ts = float(nxt.get("ts", first_ts))
                    if ts - first_ts > JOIN_WINDOW:
                        break
                    nxt = self._rx_queue.popleft()
                    ssrc_key = nxt.get("ssrc")
                    ssrc_key = ssrc_key if ssrc_key is not None else id(nxt)
                    if ssrc_key in seen_ssrc:
                        # Keep sequential frames from the same speaker queued, but continue
                        # scanning for overlapping talkers within the window so they layer cleanly.
                        defer_same.append(nxt)
                        continue
                    seen_ssrc.add(ssrc_key)
                    batch.append(nxt)
                while defer_same:
                    # Preserve original order for deferred frames
                    self._rx_queue.appendleft(defer_same.pop())
        except Exception:
            return None

        try:
            mix = np.zeros(AUDIO_BLOCK, dtype=np.float32)
            for item in batch:
                arr = np.asarray(item.get("buf"), dtype=np.float32).reshape(-1)
                chan_idx = item.get("chan_idx")
                try:
                    if chan_idx is not None and not self.channel_is_audible(int(chan_idx)):
                        continue
                except Exception:
                    pass
                try:
                    gain = self.get_channel_volume(int(chan_idx)) if chan_idx is not None else self.get_channel_volume(int(self.active_chan.get()))
                except Exception:
                    gain = 1.0
                mix[:len(arr)] += arr * float(gain)

            # Soft-limit to prevent clipping when multiple talkers overlap
            peak = float(np.max(np.abs(mix))) if mix.size else 0.0
            if peak > 1.0:
                mix /= peak
            try:
                self._rx_last_frame = mix.copy()
                self._rx_last_repeat = 0
            except Exception:
                pass
            return mix
        except Exception:
            return None

    def _combo_active_now(self):
        return self._combo_is_active(self.ptt_combos)

    # ---------- Channel OSD ----------
    def _show_channel_osd(self):
        try:
            if hasattr(self, "_osd") and self._osd is not None:
                try: self._osd.destroy()
                except Exception: pass
                self._osd = None
            self._osd = tk.Toplevel(self.root)
            self._osd.overrideredirect(True); self._osd.attributes("-topmost", True)
            msg = self._active_chan_label()
            lbl = tk.Label(self._osd, text=msg, bg="#222", fg="#fff", padx=12, pady=6, font=("Segoe UI", 12, "bold")); lbl.pack()
            self.root.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            self._osd.update_idletasks()
            ow, oh = self._osd.winfo_width(), self._osd.winfo_height()
            x = rx + (rw - ow)//2; y = ry + rh - oh - 40
            self._osd.geometry(f"{ow}x{oh}+{x}+{y}")
            self._osd.after(900, lambda: (self._osd.destroy() if self._osd else None))
        except Exception:
            pass

    # ---------- Binding ----------
    def _combo_attr_for(self, target):
        return {
            "ptt": "ptt_combos",
            "next": "next_combos",
            "prev": "prev_combos",
            "vol_up": "vol_up_combos",
            "vol_down": "vol_down_combos",
            "chan_a": "chan_a_combos",
            "chan_b": "chan_b_combos",
            "chan_c": "chan_c_combos",
            "chan_d": "chan_d_combos",
        }.get(target)

    def _pretty_combo_name(self, target):
        return {
            "ptt": "PTT",
            "next": "Next Channel",
            "prev": "Previous Channel",
            "vol_up": "Volume Up",
            "vol_down": "Volume Down",
            "chan_a": "Channel A",
            "chan_b": "Channel B",
            "chan_c": "Channel C",
            "chan_d": "Channel D",
        }.get(target, target)

    def _refresh_combo_label(self, target):
        display = self._combo_list_to_display(self._get_combo_list_for(target))
        if target == 'ptt' and hasattr(self, 'ptt_combo_label'):
            self.ptt_combo_label.config(text=f"PTT Combo(s): {display}")
        elif target == 'next' and hasattr(self, 'next_combo_label'):
            self.next_combo_label.config(text=f"Next: {display}")
        elif target == 'prev' and hasattr(self, 'prev_combo_label'):
            self.prev_combo_label.config(text=f"Prev: {display}")
        elif target == 'vol_up' and hasattr(self, 'vol_up_combo_label'):
            self.vol_up_combo_label.config(text=f"Vol +: {display}")
        elif target == 'vol_down' and hasattr(self, 'vol_down_combo_label'):
            self.vol_down_combo_label.config(text=f"Vol −: {display}")
        elif target == 'chan_a' and hasattr(self, 'chan_a_combo_label'):
            self.chan_a_combo_label.config(text=f"A: {display}")
        elif target == 'chan_b' and hasattr(self, 'chan_b_combo_label'):
            self.chan_b_combo_label.config(text=f"B: {display}")
        elif target == 'chan_c' and hasattr(self, 'chan_c_combo_label'):
            self.chan_c_combo_label.config(text=f"C: {display}")
        elif target == 'chan_d' and hasattr(self, 'chan_d_combo_label'):
            self.chan_d_combo_label.config(text=f"D: {display}")

    def _set_combo_list_for(self, target, combos):
        attr = self._combo_attr_for(target)
        if not attr:
            return []
        normalized = self._normalize_combo_list(combos)
        setattr(self, attr, normalized)
        self._refresh_combo_label(target)
        return normalized

    def _get_combo_list_for(self, target):
        attr = self._combo_attr_for(target)
        if not attr:
            return []
        return self._normalize_combo_list(getattr(self, attr, []))

    def _reset_bind_state(self):
        self._waiting_bind = False
        self._waiting_bind_for = None
        self._bind_candidate = frozenset()
        self._bind_mode = None
        self._bind_replace_index = None
        self._bind_seen_input = False
        self._bind_last_non_empty = frozenset()
        self._bind_use_global = False

    def _ask_slot(self, pretty_name, combos, verb):
        summary = "\n".join(f"{i+1}) {self._combo_to_string(c) or 'None'}" for i, c in enumerate(combos))
        prompt = f"{verb} which {pretty_name} keybind slot? (1-{len(combos)})\n\n{summary}"
        idx = simpledialog.askinteger(f"{verb} keybind", prompt, parent=self.root, minvalue=1, maxvalue=len(combos))
        if idx is None:
            return None
        return int(idx) - 1

    def _prompt_bind_action(self, pretty, combos, can_add):
        """Modal dialog offering Add / Replace / Delete with labeled buttons."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Change keybind")
        dlg.transient(self.root)
        dlg.grab_set()
        palette = getattr(self, "_palette", {}) or {}
        try:
            bg = palette.get("surface", palette.get("bg"))
            if bg:
                dlg.configure(bg=bg, highlightbackground=bg, highlightcolor=bg)
        except Exception:
            pass
        ttk.Label(dlg, text=f"{pretty} keybinds: {self._combo_list_to_display(combos)}").grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12,6))
        ttk.Label(dlg, text="Choose what to do:").grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0,10))

        result = {"action": None}
        def _set_action(val):
            result["action"] = val
            dlg.destroy()

        btn_add = ttk.Button(dlg, text="Add", command=lambda: _set_action("add"))
        if not can_add:
            btn_add.state(["disabled"])
        btn_add.grid(row=2, column=0, padx=8, pady=(0,12))
        ttk.Button(dlg, text="Replace", command=lambda: _set_action("replace")).grid(row=2, column=1, padx=8, pady=(0,12))
        ttk.Button(dlg, text="Delete", command=lambda: _set_action("delete")).grid(row=2, column=2, padx=8, pady=(0,12))
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).grid(row=2, column=3, padx=8, pady=(0,12))

        for c in range(4):
            dlg.columnconfigure(c, weight=1)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.wait_window()
        return result["action"]

    def _start_combo_bind(self, target="ptt"):
        combos = self._get_combo_list_for(target)
        pretty = self._pretty_combo_name(target)
        can_add = len(combos) < 3

        action = self._prompt_bind_action(pretty, combos, can_add)
        if not action:
            self._reset_bind_state()
            return

        # Immediate delete (no capture needed)
        if action == "delete":
            if not combos:
                messagebox.showinfo("Delete keybind", f"No {pretty} keybinds to delete.", parent=self.root)
                self._reset_bind_state()
                return
            idx = self._ask_slot(pretty, combos, "Delete")
            if idx is None:
                self._reset_bind_state()
                return
            try:
                combos.pop(idx)
                self._set_combo_list_for(target, combos)
                self._persist_combo_settings()
                if hasattr(self, "bind_hint"):
                    self.bind_hint.config(text=f"Deleted {pretty} slot {idx+1}.")
                    self.root.after(1200, lambda: self.bind_hint.config(text=""))
            except Exception:
                pass
            self._reset_bind_state()
            return

        replace_idx = None
        if action == "replace" and combos:
            replace_idx = self._ask_slot(pretty, combos, "Replace")
            if replace_idx is None:
                self._reset_bind_state()
                return

        self._waiting_bind = True
        self._waiting_bind_for = target
        self._bind_candidate = frozenset()
        self._bind_seen_input = False
        self._bind_last_non_empty = frozenset()
        self._bind_mode = action
        self._bind_replace_index = replace_idx

        # Prefer global capture when available
        self._bind_use_global = False
        try:
            if hasattr(self, "global_keys") and self.global_keys:
                started = self.global_keys.begin_capture(
                    target,
                    on_done=self._capture_done,
                    on_cancel=self._capture_cancel,
                    release_window_ms=400,
                    timeout_ms=10000,
                )
                self._bind_use_global = bool(started)
        except Exception:
            self._bind_use_global = False

        if hasattr(self, 'bind_hint'):
            action_txt = "Adding new" if action == "add" else f"Replacing #{(replace_idx or 0)+1}"
            prefix = "Listening (global)…" if self._bind_use_global else "Listening…"
            self.bind_hint.config(text=f"{action_txt}: {prefix} press combo, release to save")

    def _finalize_combo_bind(self):
        """Finalize a pending combo bind using the last candidate tokens."""
        try:
            name = self._waiting_bind_for
            tokens = self._bind_candidate or frozenset()
            mode = self._bind_mode or "replace"
            replace_idx = self._bind_replace_index

            combos = self._get_combo_list_for(name)
            if tokens:
                if mode == "add":
                    combos.append(tokens)
                else:
                    if replace_idx is None or replace_idx >= len(combos):
                        replace_idx = len(combos) - 1 if combos else 0
                    if combos:
                        combos[replace_idx] = tokens
                    else:
                        combos = [tokens]
            else:
                if mode == "replace" and combos:
                    if replace_idx is None or replace_idx >= len(combos):
                        replace_idx = len(combos) - 1
                    if replace_idx is not None and 0 <= replace_idx < len(combos):
                        combos.pop(replace_idx)

            combos = self._set_combo_list_for(name, combos)

            # Persist to config
            try:
                self._persist_combo_settings()
            except Exception:
                pass

            # Hint
            if hasattr(self, 'bind_hint'):
                self.bind_hint.config(text="Saved.")
                self.root.after(1200, lambda: self.bind_hint.config(text=""))
        finally:
            self._reset_bind_state()

    def _persist_combo_settings(self):
        data = load_user_config()
        data = self._write_combo_config(data)
        save_user_config(data)

    def _restart_global_keys(self):
        def _do():
            try:
                if hasattr(self, 'global_keys') and self.global_keys:
                    self.global_keys.stop()
                    self.global_keys.start()
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self.root.after(0, _do)
        except Exception:
            pass

    def _on_key_press(self, event):
        tok_norm = self._normalize_key(event.keysym)
        if self._is_token_ignored(tok_norm):
            return
        self._pressed.add(event.keysym)
        if self._waiting_bind:
            # Track the current pressed set as candidate (normalized)
            self._bind_candidate = self._current_tokens()
            if hasattr(self, 'bind_hint'):
                slot_txt = ""
                if self._bind_mode == "replace" and self._bind_replace_index is not None:
                    slot_txt = f" slot {self._bind_replace_index+1}"
                self.bind_hint.config(text=f"Listening{slot_txt}… {self._combo_to_string(self._bind_candidate) or ''}")
        if self._input_debug_enabled and not have_pynput() and tok_norm:
            self._enqueue_input_debug_event("Keyboard", tok_norm, "down", detail="tk-local")
        if not have_pynput():
            self._request_input_refresh(source='local-press')

    def _on_key_release(self, event):
        tok_norm = self._normalize_key(event.keysym)
        if tok_norm and self._is_token_ignored(tok_norm):
            if event.keysym in self._pressed:
                self._pressed.remove(event.keysym)
            return
        if event.keysym in self._pressed:
            self._pressed.remove(event.keysym)
        if self._waiting_bind and not self._pressed:
            # Finalize once everything released
            self._finalize_combo_bind()
            return
        if self._input_debug_enabled and not have_pynput() and tok_norm:
            self._enqueue_input_debug_event("Keyboard", tok_norm, "up", detail="tk-local")
        if not have_pynput():
            self._request_input_refresh(source='local-release')

    def _maybe_finalize_bind_poll(self):
        if self._bind_use_global:
            return
        if not self._waiting_bind:
            return
        tokens = self._current_tokens()
        if tokens:
            self._bind_seen_input = True
            self._bind_candidate = tokens
            self._bind_last_non_empty = tokens
        if hasattr(self, "bind_hint"):
            slot_txt = ""
            if self._bind_mode == "replace" and self._bind_replace_index is not None:
                slot_txt = f" slot {self._bind_replace_index+1}"
            display_tokens = self._bind_candidate if tokens else self._bind_last_non_empty
            self.bind_hint.config(text=f"Listening{slot_txt}… {self._combo_to_string(display_tokens) or ''}")
        if not tokens and self._bind_seen_input:
            # Use the last non-empty combo when releasing
            if self._bind_last_non_empty:
                self._bind_candidate = self._bind_last_non_empty
            self._finalize_combo_bind()

    def _capture_done(self, target, combo):
        try:
            tokens = frozenset(self._normalize_token(t) for t in (combo or []) if t)
        except Exception:
            tokens = frozenset()
        def _apply():
            self._bind_candidate = tokens
            self._finalize_combo_bind()
        try:
            self.root.after(0, _apply)
        except Exception:
            _apply()

    def _capture_cancel(self, target):
        def _apply():
            if hasattr(self, "bind_hint"):
                self.bind_hint.config(text="Bind cancelled.")
                self.root.after(1200, lambda: self.bind_hint.config(text=""))
            self._reset_bind_state()
        try:
            self.root.after(0, _apply)
        except Exception:
            _apply()

    def _mouse_token_from_event(self, event):
        num = getattr(event, "num", None)
        if num == 1: return "MouseLeft"
        if num == 2: return "MouseMiddle"
        if num == 3: return "MouseRight"
        if num == 4: return "MouseX1"
        if num == 5: return "MouseX2"
        return None

    def _on_mouse_press(self, event):
        tok = self._mouse_token_from_event(event)
        if not tok:
            return
        if self._is_token_ignored(tok):
            return
        self._pressed.add(tok)
        if self._waiting_bind:
            self._bind_candidate = self._current_tokens()
            if hasattr(self, 'bind_hint'):
                slot_txt = ""
                if self._bind_mode == "replace" and self._bind_replace_index is not None:
                    slot_txt = f" slot {self._bind_replace_index+1}"
                self.bind_hint.config(text=f"Listening{slot_txt}… {self._combo_to_string(self._bind_candidate) or ''}")
        if self._input_debug_enabled and not have_pynput():
            self._enqueue_input_debug_event("Mouse", tok, "down", detail="tk-local")
        if not have_pynput():
            self._request_input_refresh(source='local-mouse-press')

    def _on_mouse_release(self, event):
        tok = self._mouse_token_from_event(event)
        if tok and self._is_token_ignored(tok):
            if tok in self._pressed:
                self._pressed.remove(tok)
            return
        if tok and tok in self._pressed:
            self._pressed.remove(tok)
        if self._input_debug_enabled and not have_pynput() and tok:
            self._enqueue_input_debug_event("Mouse", tok, "up", detail="tk-local")
        if not have_pynput():
            self._request_input_refresh(source='local-mouse-release')

    # ---------- PTT + channel cycling + volume bump ----------
    def _update_ptt_and_channels(self, source=''):
        tokens = (self._global_pressed_tokens() if have_pynput() and not self._waiting_bind else self._current_tokens())
        if tokens == self._last_input_tokens:
            return
        self._last_input_tokens = tokens

        active = self._combo_is_active(self.ptt_combos, tokens=tokens)
        prev_ptt = self.ptt.get()
        if self.ptt_mode.get() == "hold":
            self.ptt.set(active)
        else:
            if active and not self._combo_active_prev:
                self.ptt.set(not self.ptt.get())
        now_ptt = self.ptt.get()

        # NEW: log any PTT state change so we can see when the app thinks we're keyed
        if now_ptt != prev_ptt:
            print(f"[CLIENT][PTT] state changed -> {now_ptt} (mode={self.ptt_mode.get()}, source={source})")

        if (not prev_ptt) and now_ptt: self.sounds.play_keyup()
        elif prev_ptt and (not now_ptt): self.sounds.play_unkey()
        self._combo_active_prev = active

        chan_combo_list = [
            self.chan_a_combos,
            self.chan_b_combos,
            self.chan_c_combos,
            self.chan_d_combos,
        ]
        direct_active = False
        for idx, combos in enumerate(chan_combo_list):
            now = self._combo_is_active(combos, tokens=tokens)
            if now:
                direct_active = True
            if now and not self._edge_chan_select[idx]:
                self._activate_channel(idx)
            self._edge_chan_select[idx] = bool(now)

        next_now = self._combo_is_active(self.next_combos, tokens=tokens)
        prev_now = self._combo_is_active(self.prev_combos, tokens=tokens)
        if not direct_active:
            if next_now and not self._edge_next_prev["next"]: self._cycle_next()
            if prev_now and not self._edge_next_prev["prev"]: self._cycle_prev()
        self._edge_next_prev["next"] = bool(next_now); self._edge_next_prev["prev"] = bool(prev_now)

        up_now = self._combo_is_active(self.vol_up_combos, tokens=tokens)
        down_now = self._combo_is_active(self.vol_down_combos, tokens=tokens)
        if up_now and not self._edge_vol["up"]: self._bump_active_volume(+10)
        if down_now and not self._edge_vol["down"]: self._bump_active_volume(-10)
        self._edge_vol["up"] = bool(up_now); self._edge_vol["down"] = bool(down_now)

        # Reflect current PTT to UDP
        desired_ptt = bool(self.ptt.get())
        try:
            if hasattr(self, "_udp") and self._udp:
                if self._last_udp_ptt is None or bool(self._last_udp_ptt) != desired_ptt:
                    self._udp.set_ptt(desired_ptt)
                    self._last_udp_ptt = desired_ptt
        except Exception:
            pass


    # ------------- Connection Tab logic -------------
    def _on_connect_click(self):
        """Connect (or reconnect) the UDP voice client."""
        # Tear down any existing UDP client
        try:
            if hasattr(self, "_udp") and self._udp:
                self._udp.stop()
                self._udp = None
        except Exception:
            self._udp = None
        self._last_udp_ptt = None
        self._last_update_tag = None

        # Resolve server address
        ip = self.server_ip.get().strip() if hasattr(self, "server_ip") else "127.0.0.1"
        try:
            port = int(self.server_port.get().strip()) if hasattr(self, "server_port") else 8765
        except Exception:
            port = 8765

        # Quick UDP presence poll so we don't claim to be connected when the server is down.
        reachable, reason = UdpVoiceClient.probe_server(ip, port, timeout=1.5)
        if not reachable:
            if hasattr(self, "status_text"):
                self.status_text.set(f"UDP server not responding at {ip}:{port}: {reason}")
            try:
                if hasattr(self, "connected"):
                    self.connected.set(False)
                self._update_connection_indicator()
            except Exception:
                pass
            try:
                print(f"[CLIENT][UDP] probe to {ip}:{port} failed: {reason}")
            except Exception:
                pass
            return

        # Derive SSRC / client_id from Steam ID override if present
        try:
            steam_txt = (self.steam_ssrc_var.get() or "").strip()
        except Exception:
            steam_txt = ""
        ssrc = None
        client_id = None

        if steam_txt:
            if steam_txt.isdigit():
                try:
                    ssrc = int(steam_txt) & 0xFFFFFFFF
                    client_id = steam_txt
                except Exception:
                    ssrc = None
            else:
                print(f"[CLIENT][SSRC] invalid Steam ID '{steam_txt}' (not numeric); falling back to random SSRC")

        if ssrc is None:
            import time as _time, random as _random
            # Mix time and random to reduce collision risk
            ssrc = (int(_time.time()) ^ _random.randint(1, 0x7FFFFFFF)) & 0xFFFFFFFF

        # Create UDP client and start REGISTER + RX + heartbeat
        try:
            self._udp = UdpVoiceClient(
                ip,
                port,
                ssrc=ssrc,
                nick=self.callsign_var.get().strip() if hasattr(self, "callsign_var") else "client",
                net=self.network.get().strip() if hasattr(self, "network") else "NET-1",
                on_log=self._udp_log,
                client_id=client_id,
            )
            # Attach log callback if supported by the client
            try:
                self._udp.on_log = self._udp_log
            except Exception:
                pass
            print(f"[CLIENT][UDP] created UdpVoiceClient to {ip}:{port} ssrc={ssrc} client_id={client_id}")
            self._udp.start()
        except Exception as e:
            if hasattr(self, "status_text"):
                self.status_text.set(f"UDP connect failed: {e}")
            print(f"[CLIENT][UDP][ERROR] connect failed: {e}")
            return

        # Gather A–D frequencies with safe fallbacks
        try:
            freqs = [
                float(self.chan_vars[0].get()),
                float(self.chan_vars[1].get()),
                float(self.chan_vars[2].get()),
                float(self.chan_d_var.get()),
            ]
        except Exception:
            try:
                freqs = [float(x) for x in getattr(self, "chan_freqs", [0.0, 0.0, 0.0, 0.0])]
            except Exception:
                freqs = [0.0, 0.0, 0.0, 0.0]

        # Active channel index
        try:
            active_idx = int(self.active_chan.get())
        except Exception:
            active_idx = int(getattr(self, "active_channel_idx", 0) or 0)
        active_idx = max(0, min(3, active_idx))

        # Scan enabled? Include per-channel scan flags.
        scan, scan_channels = self._current_scan_state()

        # Push initial channel state using udp_client.update_channels (JSON)
        try:
            if hasattr(self, "_udp") and self._udp:
                state = {
                    "active_channel": active_idx,
                    "freqs": freqs,
                    "scan": bool(scan),
                    "scan_channels": scan_channels,
                }
                self._udp.update_channels(state)
        except Exception:
            pass

        # Reflect current PTT into UDP client
        try:
            if hasattr(self, "_udp") and self._udp:
                current_ptt = bool(self.ptt.get())
                self._udp.set_ptt(current_ptt)
                self._last_udp_ptt = current_ptt
        except Exception:
            pass

        # Audio RX callback: enqueue into jitter buffer (played in main loop)
        def _rx_audio_cb(buf_f32, rate, src_ssrc=None, chan_idx=None):
            try:
                # Drop our own frames if loopback is disabled
                try:
                    if (not bool(self.loopback_enabled.get())) and src_ssrc is not None and self.my_ssrc is not None and int(src_ssrc) == int(self.my_ssrc):
                        return
                except Exception:
                    pass
                self._enqueue_rx_frame(buf_f32, rate, chan_idx=chan_idx, src_ssrc=src_ssrc)
                try:
                    import time as _time
                    self.rx_active_recent_ts = _time.time()
                except Exception:
                    pass
                # Enqueue lamp updates for UI thread
                try:
                    targets = []
                    try:
                        if chan_idx is not None:
                            targets = [int(chan_idx)]
                    except Exception:
                        targets = []
                    if not targets:
                        try:
                            targets = list(self.audible_channels())
                        except Exception:
                            targets = []
                    if not targets:
                        try:
                            targets = [int(self.active_chan.get())]
                        except Exception:
                            targets = []
                    if targets:
                        try:
                            with self._rx_lamp_lock:
                                self._rx_lamp_queue.extend(targets)
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if hasattr(self, "_udp") and self._udp:
                self._udp.on_rx_audio = _rx_audio_cb
        except Exception:
            pass

        def _rx_ctrl_cb(msg):
            try:
                ctype = msg.get("type")
                raw = msg.get("data", b"")
                if ctype == CTRL_UPDATE_OFFER:
                    self._handle_update_offer(raw)
            except Exception:
                pass

        try:
            if hasattr(self, "_udp") and self._udp:
                self._udp.on_rx_ctrl = _rx_ctrl_cb
        except Exception:
            pass

        # Clear any stale RX jitter buffer on new connect
        try:
            with self._rx_lock:
                self._rx_queue.clear()
        except Exception:
            pass

        if hasattr(self, "connected"):
            self.connected.set(True)
        if hasattr(self, "status_text"):
            self.status_text.set(f"UDP connected to {ip}:{port} (SSRC={ssrc})")

        # Refresh connection indicator lamp/label
        try:
            self._update_connection_indicator()
        except Exception:
            pass

        # Track our SSRC and apply loopback filter setting
        try:
            self.my_ssrc = ssrc
            self._apply_loopback_setting()
        except Exception:
            pass

        # Auto-start the audio loop on successful connect so TX frames actually flow
        try:
            if not getattr(self, "running", False):
                print("[CLIENT][AUDIO] auto-starting audio loop after UDP connect")
                self.start()
        except Exception as e:
            print(f"[CLIENT][AUDIO][ERROR] auto-start failed: {e}")

    def _on_steam_ssrc_save(self):
        """Persist Steam ID/SSRC override and, if connected, reconnect with new SSRC."""
        try:
            txt = (self.steam_ssrc_var.get() or "").strip()
        except Exception:
            txt = ""

        # Allow clearing the override
        if not txt:
            data = load_user_config()
            if "steam_ssrc" in data:
                data.pop("steam_ssrc", None)
                save_user_config(data)
            if hasattr(self, "status_text"):
                self.status_text.set("Cleared Steam ID / SSRC override.")
            # No reconnect needed; next connect will use random SSRC
            return

        if not txt.isdigit():
            if hasattr(self, "status_text"):
                self.status_text.set("Steam ID must be numeric (SteamID64).")
            return

        # Save to config
        data = load_user_config()
        data["steam_ssrc"] = txt
        save_user_config(data)
        if hasattr(self, "status_text"):
            self.status_text.set(f"Saved Steam ID / SSRC: {txt}")

        # If UDP client is already running, push the new Steam ID to the server
        # and then reconnect so the SSRC + REGISTER also reflect it.
        if getattr(self, "_udp", None):
            try:
                # First, try a live presence update (no reconnect required for admin_app).
                try:
                    if hasattr(self._udp, "update_client_id"):
                        self._udp.update_client_id(txt)
                except Exception:
                    pass

                print(f"[CLIENT][SSRC] Steam ID changed, reconnecting with SSRC={txt}")
                self._on_connect_click()
            except Exception as e:
                print(f"[CLIENT][SSRC] error while reconnecting with new SSRC: {e}")
    def _on_disconnect_click(self):
        """Disconnect UDP voice client and update UI."""
        try:
            if hasattr(self, '_udp') and self._udp:
                self._udp.stop()
                self._udp = None
        except Exception:
            self._udp = None
        if hasattr(self, 'connected'):
            self.connected.set(False)
        if hasattr(self, 'status_text'):
            self.status_text.set('Disconnected.')
        try:
            self._update_connection_indicator()
        except Exception:
            pass

    # ------------- Update handling -------------
    def _handle_update_offer(self, raw_payload: bytes):
        """Prompt the user when the server announces a new client update."""
        try:
            info = json.loads(raw_payload.decode("utf-8", "ignore"))
        except Exception:
            return
        if not isinstance(info, dict):
            return
        url = info.get("url")
        name = info.get("name") or "client_update.exe"
        size = info.get("size") or 0
        tag = info.get("sha256") or info.get("uploaded_at") or info.get("name") or ""
        # If the server advertises a version and it matches ours, skip the update.
        server_ver = None
        try:
            if isinstance(info.get("version"), str):
                server_ver = info.get("version").strip()
        except Exception:
            server_ver = None
        if not server_ver:
            try:
                import re
                m = re.search(r"(\d+(?:\.\d+){1,3})", name)
                if m:
                    server_ver = m.group(1)
            except Exception:
                server_ver = None
        client_ver = APP_VERSION
        if server_ver and client_ver and server_ver == client_ver:
            try:
                self._last_update_tag = tag or server_ver
            except Exception:
                pass
            try:
                if hasattr(self, "status_text"):
                    self.status_text.set(f"Update skipped: server version {server_ver} matches client {client_ver}.")
            except Exception:
                pass
            return
        if tag and self._last_update_tag == tag:
            return
        self._last_update_tag = tag or self._last_update_tag

        def prompt():
            if not url:
                messagebox.showerror("Update", "Server announced an update but did not include a download URL.")
                return
            size_msg = ""
            try:
                if size:
                    size_mb = float(size) / (1024 * 1024)
                    size_msg = f" ({size_mb:.2f} MB)"
            except Exception:
                size_msg = ""
            msg = f"A client update is available:\n{name}{size_msg}\n\nUpdate now?"
            res = messagebox.askyesno("Client Update Available", msg)
            if not res:
                try:
                    if getattr(self, "_udp", None):
                        self._udp.send_update_response(False, "declined")
                except Exception:
                    pass
                self._on_disconnect_click()
                return
            try:
                if getattr(self, "_udp", None):
                    self._udp.send_update_response(True, "accepted")
            except Exception:
                pass
            threading.Thread(target=self._download_and_apply_update, args=(info,), daemon=True).start()

        try:
            self.root.after(0, prompt)
        except Exception:
            prompt()

    def _download_and_apply_update(self, info: dict):
        """Download update payload, verify, and launch the installer/exe."""
        url = info.get("url")
        name = info.get("name") or "client_update.exe"
        sha = info.get("sha256")
        expected_size = info.get("size")
        try:
            expected_bytes = int(expected_size) if expected_size else None
            if expected_bytes <= 0:
                expected_bytes = None
        except Exception:
            expected_bytes = None
        if not url:
            try:
                self.root.after(0, lambda: messagebox.showerror("Update Failed", "No download URL provided."))
            except Exception:
                pass
            return

        downloads_dir = os.path.abspath(os.path.join(os.path.expanduser("~"), "Downloads"))
        try:
            os.makedirs(downloads_dir, exist_ok=True)
        except Exception:
            pass
        dest_final = os.path.abspath(os.path.join(downloads_dir, name))
        dest_tmp = dest_final + ".download"
        try:
            if os.path.isfile(dest_tmp):
                os.remove(dest_tmp)
        except Exception:
            pass

        # Progress UI helpers (runs from a worker thread; UI updates via .after)
        progress_state = {"win": None, "bar": None, "label": None}
        progress_ready = threading.Event()

        def _set_status(msg: str):
            try:
                if hasattr(self, "status_text"):
                    self.root.after(0, lambda: self.status_text.set(msg))
            except Exception:
                pass

        def _ensure_progress_ui():
            def _make():
                try:
                    win = tk.Toplevel(self.root)
                    win.title("Downloading Update")
                    win.resizable(False, False)
                    ttk.Label(win, text="Downloading update from server").grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))
                    label_var = tk.StringVar(value="Connecting to server...")
                    ttk.Label(win, textvariable=label_var, foreground=self._palette.get("muted", "#555")).grid(row=1, column=0, sticky="w", padx=12)
                    bar = ttk.Progressbar(win, orient="horizontal", mode="indeterminate", length=320)
                    bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 12))
                    bar.start(10)
                    progress_state.update({"win": win, "bar": bar, "label": label_var})
                except Exception:
                    pass
                progress_ready.set()
            try:
                self.root.after(0, _make)
            except Exception:
                progress_ready.set()

        def _update_progress(downloaded: int, total: int | None, source: str):
            def _apply():
                bar = progress_state.get("bar")
                label_var = progress_state.get("label")
                if not bar or not label_var:
                    return
                try:
                    if total and total > 0:
                        max_val = max(int(total), int(downloaded))
                        try:
                            bar.stop()
                        except Exception:
                            pass
                        bar.configure(mode="determinate", maximum=max_val)
                        bar["value"] = max(0, min(downloaded, max_val))
                        pct = (downloaded / max_val) * 100 if max_val else 0
                        label_var.set(f"{source}: {pct:.1f}% ({downloaded/1024:.1f} / {max_val/1024:.1f} KB)")
                    else:
                        bar.configure(mode="indeterminate")
                        try:
                            bar.start(10)
                        except Exception:
                            pass
                        label_var.set(f"{source}: {downloaded/1024:.1f} KB")
                    _set_status(label_var.get())
                except Exception:
                    pass
            try:
                self.root.after(0, _apply)
            except Exception:
                pass

        def _close_progress(msg: str | None = None):
            def _apply():
                bar = progress_state.get("bar")
                label_var = progress_state.get("label")
                if label_var and msg:
                    try:
                        label_var.set(msg)
                    except Exception:
                        pass
                if bar:
                    try:
                        bar.stop()
                    except Exception:
                        pass
                win = progress_state.get("win")
                if win:
                    try:
                        win.destroy()
                    except Exception:
                        pass
            try:
                self.root.after(0, _apply)
            except Exception:
                pass

        download_errors = []

        def _candidate_urls():
            urls = [url]
            try:
                parsed = urllib.parse.urlparse(url)
                host = ""
                try:
                    host = (self.server_ip.get() or "").strip()
                except Exception:
                    host = ""
                if host:
                    port = parsed.port
                    netloc = host if not port else f"{host}:{port}"
                    alt = parsed._replace(netloc=netloc)
                    alt_url = alt.geturl()
                    if alt_url not in urls:
                        urls.append(alt_url)
            except Exception:
                pass
            return urls

        _set_status(f"Downloading update: {name}")
        _ensure_progress_ui()

        chosen_url = None
        downloaded = 0
        total_bytes = expected_bytes
        for candidate in _candidate_urls():
            downloaded = 0
            total_bytes = expected_bytes
            try:
                progress_ready.wait(timeout=2.0)
                with urllib.request.urlopen(candidate, timeout=20) as resp, open(dest_tmp, "wb") as f:
                    try:
                        hdr_len = int(resp.headers.get("Content-Length", "0"))
                        if hdr_len > 0:
                            total_bytes = hdr_len
                    except Exception:
                        pass
                    _update_progress(0, total_bytes, candidate)
                    for chunk in iter(lambda: resp.read(65536), b""):
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        _update_progress(downloaded, total_bytes, candidate)
                if total_bytes and downloaded < total_bytes:
                    raise IOError(f"incomplete download ({downloaded}/{total_bytes} bytes)")
                chosen_url = candidate
                _update_progress(downloaded, total_bytes or downloaded, candidate)
                break
            except Exception as e:
                download_errors.append(f"{candidate} -> {e}")
                try:
                    os.remove(dest_tmp)
                except Exception:
                    pass

        if not chosen_url:
            msg = "; ".join(download_errors) if download_errors else "Unknown error"
            try:
                _close_progress("Download failed")
                self.root.after(0, lambda: messagebox.showerror("Update Failed", f"Download failed: {msg}"))
            except Exception:
                pass
            return

        try:
            print(f"[CLIENT][UPDATE] downloaded {name} from {chosen_url}")
        except Exception:
            pass

        try:
            downloaded = os.path.getsize(dest_tmp)
        except Exception:
            pass
        _update_progress(downloaded, total_bytes or expected_bytes or downloaded, "Verifying download")

        # Optional size check
        if expected_bytes:
            try:
                actual = os.path.getsize(dest_tmp)
                if int(actual) != int(expected_bytes):
                    self.root.after(0, lambda: messagebox.showwarning("Update Warning", f"Expected {expected_bytes} bytes, got {actual}. Continuing."))
            except Exception:
                pass

        # Optional SHA256 verification
        if sha:
            try:
                h = hashlib.sha256()
                with open(dest_tmp, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        if not chunk:
                            break
                        h.update(chunk)
                if h.hexdigest() != sha:
                    try:
                        os.remove(dest_tmp)
                    except Exception:
                        pass
                    _close_progress("Checksum mismatch")
                    self.root.after(0, lambda: messagebox.showerror("Update Failed", "Checksum mismatch; update aborted."))
                    return
            except Exception as e:
                _close_progress("Checksum failed")
                self.root.after(0, lambda: messagebox.showerror("Update Failed", f"Checksum failed: {e}"))
                return

        # Move the verified download into place in Downloads
        try:
            os.replace(dest_tmp, dest_final)
        except Exception:
            try:
                shutil.move(dest_tmp, dest_final)
            except Exception as e:
                try:
                    _close_progress("Save failed")
                    self.root.after(0, lambda: messagebox.showerror("Update Failed", f"Could not save update: {e}"))
                except Exception:
                    pass
                return

        _close_progress("Download complete")
        try:
            self.root.after(0, lambda: self.status_text.set(f"Update saved to {dest_final}"))
            self.root.after(0, lambda: messagebox.showinfo("Update Downloaded", f"Saved to:\n{dest_final}\nPlease move/replace it where you want it."))
        except Exception:
            pass

    # ------------- Audio loop -------------
    def _start_audio_loop(self):
        if self.running: return
        cfg = load_user_config()
        cfg["selected_devices"] = {"input_label": self.input_dev.get(), "output_label": self.output_dev.get()}
        save_user_config(cfg)
        in_dev = None; out_dev = None
        try:
            if self.input_dev.get() != "Default": in_dev = int(self.input_dev.get().split(":")[0])
            if self.output_dev.get() != "Default": out_dev = int(self.output_dev.get().split(":")[0])
        except Exception:
            messagebox.showwarning("Device Parse", "Could not parse selected device IDs; using defaults.")

        self.engine = AudioEngine(
            input_device=in_dev,
            output_device=out_dev,
            samplerate=AUDIO_RATE,
            blocksize=AUDIO_BLOCK,
        )
        try:
            self.engine.start()
        except Exception as e:
            messagebox.showerror("Audio Error", f"Could not start audio: {e}"); return
        self.effects = EdgeEffects(); self.sounds.ensure_init()
        self.running = True; self.status_text.set("Audio loop running.")
        self.worker_thread = threading.Thread(target=self.loop, daemon=True); self.worker_thread.start()

    def stop(self):
        if not self.running: return
        self.running = False; self.engine.stop()
        self.status_text.set("Audio loop stopped.")

    def on_close(self):
        self.stop()
        try: self.global_keys.stop()
        except Exception: pass
        try:
            if getattr(self, "overlay", None):
                self.overlay.close()
        except Exception:
            pass
        try:
            if getattr(self, "_input_debugger", None):
                self._input_debugger.close()
        except Exception:
            pass
        self.root.destroy()

    def loop(self):
        """Main audio TX loop: capture from engine and send over UDP when PTT is active."""
        print("[CLIENT][LOOP] audio loop started")
        debug_counter = 0
        ptt_debug_counter = 0
        while self.running:
            # 1) Read from audio engine
            try:
                frame = self.engine.read_frame()
            except Exception as e:
                print(f"[CLIENT][AUDIO][READ_ERROR] {e}")
                time.sleep(0.01)
                continue

            if frame is None:
                time.sleep(0.001)
                continue

            # 2) Occasionally show mic RMS so we know capture is alive
            debug_counter += 1
            if debug_counter >= 50:  # roughly once per second at 20 ms frames
                debug_counter = 0
                try:
                    import numpy as np
                    buf_for_rms = frame
                    if hasattr(buf_for_rms, "ndim") and getattr(buf_for_rms, "ndim", 1) > 1:
                        buf_for_rms = buf_for_rms.mean(axis=1)
                    if len(buf_for_rms) > 0:
                        rms = float((buf_for_rms.astype("float32") ** 2).mean()) ** 0.5
                    else:
                        rms = 0.0
                    print(f"[CLIENT][AUDIO] frame RMS={rms:.6f} len={len(buf_for_rms)}")
                except Exception as e:
                    print(f"[CLIENT][AUDIO][RMS_ERROR] {e}")

            # 3) Check PTT
            if hasattr(self, "ptt") and self.ptt.get():
                buf = frame
                try:
                    if hasattr(buf, "ndim") and getattr(buf, "ndim", 1) > 1:
                        buf = buf.mean(axis=1)
                except Exception:
                    pass

                # 4) Send over UDP
                try:
                    if getattr(self, "_udp", None):
                        print("[CLIENT][LOOP] TX frame: calling _udp.send_audio(...)")
                        if hasattr(self._udp, "send_audio"):
                            self._udp.send_audio(buf.astype("float32", copy=False))
                        elif hasattr(self._udp, "send_audio_frame_f32"):
                            self._udp.send_audio_frame_f32(buf.astype("float32", copy=False))
                    else:
                        print("[CLIENT][UDP] _udp client not initialised; cannot TX")
                except Exception as e:
                    print(f"[CLIENT][UDP][ERROR] during send_audio(): {e}")

                # 5) Local sidetone
                try:
                    if bool(self.loopback_enabled.get()):
                        # With server loopback, play returned audio instead of dry sidetone.
                        if getattr(self, "_udp", None):
                            rx_loop = self._dequeue_rx_frame()
                            if rx_loop is None:
                                import numpy as np
                                rx_loop = np.zeros(AUDIO_BLOCK, dtype=np.float32)
                            self.engine.write_frame(rx_loop)
                        else:
                            self.engine.write_frame(buf)
                except Exception:
                    pass
                # Prevent RX buffer from growing stale while transmitting
                try:
                    if not bool(self.loopback_enabled.get()):
                        with self._rx_lock:
                            if len(self._rx_queue) > 50:
                                self._rx_queue.clear()
                except Exception:
                    pass
            else:
                # PTT not pressed: keep local output quiet but running, and occasionally log that we're idle
                ptt_debug_counter += 1
                if ptt_debug_counter >= 200:
                    ptt_debug_counter = 0
                    print(f"[CLIENT][LOOP] PTT is OFF, so no TX (udp_present={bool(getattr(self, '_udp', None))})")
                try:
                    rx_frame = self._dequeue_rx_frame()
                    if rx_frame is None:
                        import numpy as np
                        rx_frame = np.zeros(AUDIO_BLOCK, dtype=np.float32)
                    try:
                        import numpy as np
                        rx_frame = np.asarray(rx_frame, dtype=np.float32).reshape(-1)
                    except Exception:
                        pass
                    self.engine.write_frame(rx_frame)
                except Exception:
                    pass


    # -------- Config --------
    def _load_combo_list_from_config(self, data, base_key, extra_legacy_keys=None):
        keys_to_try = [f"{base_key}_combos", f"{base_key}_combo"]
        if extra_legacy_keys:
            keys_to_try.extend(extra_legacy_keys)
        for key in keys_to_try:
            if key in data:
                raw = data.get(key)
                if isinstance(raw, list):
                    return self._normalize_combo_list(raw), True
                if isinstance(raw, str):
                    return self._normalize_combo_list([raw]), True
                if isinstance(raw, (set, frozenset)):
                    return self._normalize_combo_list(raw), True
        return [], False

    def _write_combo_config(self, data):
        data["ptt_combos"] = self._serialize_combos(self.ptt_combos)
        data["next_combos"] = self._serialize_combos(self.next_combos)
        data["prev_combos"] = self._serialize_combos(self.prev_combos)
        data["vol_up_combos"] = self._serialize_combos(self.vol_up_combos)
        data["vol_down_combos"] = self._serialize_combos(self.vol_down_combos)
        data["chan_a_combos"] = self._serialize_combos(self.chan_a_combos)
        data["chan_b_combos"] = self._serialize_combos(self.chan_b_combos)
        data["chan_c_combos"] = self._serialize_combos(self.chan_c_combos)
        data["chan_d_combos"] = self._serialize_combos(self.chan_d_combos)
        # Legacy single-entry fallbacks (first combo only)
        data["ptt_combo"] = data["ptt_combos"][0] if data.get("ptt_combos") else ""
        data["next_combo"] = data["next_combos"][0] if data.get("next_combos") else ""
        data["prev_combo"] = data["prev_combos"][0] if data.get("prev_combos") else ""
        data["vol_up_combo"] = data["vol_up_combos"][0] if data.get("vol_up_combos") else ""
        data["vol_down_combo"] = data["vol_down_combos"][0] if data.get("vol_down_combos") else ""
        data["chan_a_combo"] = data["chan_a_combos"][0] if data.get("chan_a_combos") else ""
        data["chan_b_combo"] = data["chan_b_combos"][0] if data.get("chan_b_combos") else ""
        data["chan_c_combo"] = data["chan_c_combos"][0] if data.get("chan_c_combos") else ""
        data["chan_d_combo"] = data["chan_d_combos"][0] if data.get("chan_d_combos") else ""
        return data

    def _load_user_config_all(self):
        data = load_user_config()
        if data.get("ptt_mode") in ("hold","toggle"): self.ptt_mode.set(data["ptt_mode"])
        theme = str(data.get("ui_theme") or data.get("theme") or "").lower()
        if theme in ("light", "dark"):
            self.ui_theme.set(theme)
        joy_enabled = data.get("joystick_poller_enabled")
        if isinstance(joy_enabled, bool):
            self.joystick_enabled.set(joy_enabled)
        sfx_gain = data.get("sfx_volume")
        if isinstance(sfx_gain, (int, float)):
            try:
                g = float(sfx_gain)
            except Exception:
                g = 1.0
            g = max(0.0, min(2.0, g))
            self.sfx_gain.set(g)
            self.sfx_slider_var.set(self._sfx_slider_from_gain(g))

        ptt_loaded, ptt_found = self._load_combo_list_from_config(data, "ptt", extra_legacy_keys=["ptt_key"])
        if ptt_found: self.ptt_combos = ptt_loaded
        next_loaded, next_found = self._load_combo_list_from_config(data, "next")
        if next_found: self.next_combos = next_loaded
        prev_loaded, prev_found = self._load_combo_list_from_config(data, "prev")
        if prev_found: self.prev_combos = prev_loaded
        vol_up_loaded, vol_up_found = self._load_combo_list_from_config(data, "vol_up")
        if vol_up_found: self.vol_up_combos = vol_up_loaded
        vol_down_loaded, vol_down_found = self._load_combo_list_from_config(data, "vol_down")
        if vol_down_found: self.vol_down_combos = vol_down_loaded
        chan_a_loaded, chan_a_found = self._load_combo_list_from_config(data, "chan_a", extra_legacy_keys=["channel_a_combo", "channel_a"])
        if chan_a_found: self.chan_a_combos = chan_a_loaded
        chan_b_loaded, chan_b_found = self._load_combo_list_from_config(data, "chan_b", extra_legacy_keys=["channel_b_combo", "channel_b"])
        if chan_b_found: self.chan_b_combos = chan_b_loaded
        chan_c_loaded, chan_c_found = self._load_combo_list_from_config(data, "chan_c", extra_legacy_keys=["channel_c_combo", "channel_c"])
        if chan_c_found: self.chan_c_combos = chan_c_loaded
        chan_d_loaded, chan_d_found = self._load_combo_list_from_config(data, "chan_d", extra_legacy_keys=["channel_d_combo", "channel_d"])
        if chan_d_found: self.chan_d_combos = chan_d_loaded

        # A-C
        chans = data.get("channels")
        if isinstance(chans, list) and len(chans)==3:
            for i in range(3):
                if isinstance(chans[i], str): self.chan_vars[i].set(chans[i])
        scans = data.get("scan_flags")
        if isinstance(scans, list) and len(scans)==3:
            for i in range(3):
                try: self.scan_vars[i].set(bool(scans[i]))
                except Exception: pass
        vols = data.get("channel_volumes")
        if isinstance(vols, list) and len(vols)==3:
            for i in range(3):
                try:
                    v = int(vols[i]); v = _snap_10(v)
                    self.chan_vol_vars[i].set(v)
                except Exception: pass

        # Active channel index (0..3)
        if isinstance(data.get("active_channel_index"), int):
            self.active_chan.set(max(0,min(3,data["active_channel_index"])))

        # Devices
        sel = data.get("selected_devices", {})
        if isinstance(sel.get("input_label"), str): self.input_dev.set(sel["input_label"])
        if isinstance(sel.get("output_label"), str): self.output_dev.set(sel["output_label"])

        # Server
        server = data.get("server", {})
        if isinstance(server.get("ip"), str): self.server_ip.set(server["ip"])
        if isinstance(server.get("port"), str): self.server_port.set(server["port"])

        # Ignored inputs
        ignored = data.get("ignored_inputs", [])
        if isinstance(ignored, list):
            try:
                for t in ignored:
                    norm = self._normalize_token(t)
                    if norm:
                        self._ignored_tokens.add(norm)
            except Exception:
                pass

        # Steam SSRC override
        if "steam_ssrc" in data:
            try:
                self.steam_ssrc_var.set(str(data["steam_ssrc"]))
            except Exception:
                pass

        # Channel D
        d = data.get("channel_d", {})
        try: dv = int(d.get("volume", 50))
        except Exception: dv = 50
        self.chan_d_vol_var.set(_snap_10_min30(dv))
        # Always lock Channel D to the fixed freq/scan and persist if stale config had 000.0
        try: self.chan_d_var.set(CHANNEL_D_LOCKED_FREQ)
        except Exception: pass
        try: self.scan_d_var.set(True)
        except Exception: pass
        chan_d_cfg = {"freq": CHANNEL_D_LOCKED_FREQ, "scan": True, "volume": int(_snap_10_min30(dv))}
        if d != chan_d_cfg:
            try:
                data["channel_d"] = chan_d_cfg
                save_user_config(data)
            except Exception:
                pass

    def _save_user_config_all(self):
        data = load_user_config()
        data["ptt_mode"] = self.ptt_mode.get()
        data = self._write_combo_config(data)
        data["channels"] = [v.get() for v in self.chan_vars]               # A-C
        data["scan_flags"] = [bool(v.get()) for v in self.scan_vars]        # A-C
        data["channel_volumes"] = [int(v.get()) for v in self.chan_vol_vars]# A-C
        data["active_channel_index"] = int(self.active_chan.get())
        data.setdefault("server", {})["ip"] = self.server_ip.get().strip()
        data["server"]["port"] = self.server_port.get().strip()
        data["steam_ssrc"] = (self.steam_ssrc_var.get() or "").strip()
        data["ui_theme"] = self.ui_theme.get()
        data["joystick_poller_enabled"] = bool(self.joystick_enabled.get())
        data["sfx_volume"] = float(self.sfx_gain.get())
        data["ignored_inputs"] = sorted(self._ignored_tokens)
        data["channel_d"] = {
            "freq": CHANNEL_D_LOCKED_FREQ,
            "scan": True,
            "volume": int(_snap_10_min30(self.chan_d_vol_var.get())),
        }
        save_user_config(data)

    # -------- Devices scan helper --------
    def _populate_devices(self):
        """Fill the Input/Output device comboboxes."""
        try:
            ins, outs = scan_filtered_devices()
            in_values = ["Default"] + [f"{i}:{name}" for i, name in ins]
            out_values = ["Default"] + [f"{i}:{name}" for i, name in outs]
        except Exception:
            in_values = ["Default"]
            out_values = ["Default"]

        try:
            self.in_combo.configure(values=in_values)
            if self.input_dev.get() not in in_values:
                self.input_dev.set(in_values[0])
        except Exception:
            self.input_dev.set("Default")

        try:
            self.out_combo.configure(values=out_values)
            if self.output_dev.get() not in out_values:
                self.output_dev.set(out_values[0])
        except Exception:
            self.output_dev.set("Default")

    def _apply_loopback_setting(self):
        """Push loopback toggle to UDP client (server round-trip test)."""
        try:
            if hasattr(self, "_udp") and self._udp and hasattr(self._udp, "set_allow_loopback"):
                self._udp.set_allow_loopback(bool(self.loopback_enabled.get()))
        except Exception:
            pass

    # ------------- Public helpers -------------
    def channel_is_audible(self, idx: int) -> bool:
        if 0 <= idx < 3:
            if idx == self.active_chan.get(): return True
            return bool(self.scan_vars[idx].get())
        if idx == 3:
            return True  # D always scanned
        return False

    def audible_channels(self):
        ch = [i for i in range(3) if self.channel_is_audible(i)]
        if self.channel_is_audible(3): ch.append(3)
        return ch

    def get_channel_volume(self, idx: int) -> float:
        """Return playback gain multiplier for a given channel.
        - 50% => 1.0 (unity)
        - 51-100% linearly scales up to 2.0
        - 0-50% scales down to 0.0-1.0
        """
        def _gain(pct: int) -> float:
            pct = max(0, min(100, int(pct)))
            if pct <= 50:
                return pct / 50.0
            return 1.0 + ((pct - 50) / 50.0)  # up to 2.0

        if 0 <= idx < 3:
            v = int(self.chan_vol_vars[idx].get()); v = _snap_10(v); return _gain(v)
        elif idx == 3:
            dv = int(self.chan_d_vol_var.get()); dv = _snap_10_min30(dv); return _gain(dv)
        return 1.0

    # ------------- Debug RX engine -------------
    def _toggle_debug_rx(self):
        if not self.debug_rx_enabled.get():
            self.debug_rx_enabled.set(True)
            self.debug_btn.config(text="Stop Active Channel Debug")
            self.debug_status.set("Status: running")
            self._debug_stop.clear()
            self._debug_thread = threading.Thread(target=self._debug_loop, daemon=True)
            self._debug_thread.start()
        else:
            self._stop_debug_rx()

    def _stop_debug_rx(self):
        self.debug_rx_enabled.set(False)
        self.debug_btn.config(text="Start Active Channel Debug")
        self.debug_status.set("Status: idle")
        self._debug_stop.set()
        for i in range(4):
            self.set_rx_channel_state(i, False)

    def _debug_loop(self):
        while not self._debug_stop.is_set():
            k = random.choice([1,2])
            picks = random.sample([0,1,2,3], k=k)
            for idx in picks:
                self.set_rx_channel_state(idx, True)
            dur = random.uniform(0.8, 2.0)
            end_time = time.time() + dur
            while time.time() < end_time and not self._debug_stop.is_set():
                time.sleep(0.05)
            for idx in picks:
                self.set_rx_channel_state(idx, False)
            gap = random.uniform(0.5,1.2)
            for _ in range(int(gap/0.05)):
                if self._debug_stop.is_set(): break
                time.sleep(0.05)

print('[CLIENT][VERIFY] app compiled successfully')
