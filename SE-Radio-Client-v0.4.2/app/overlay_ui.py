
# overlay_ui.py — PNG-skinned Digital Radio Overlay (half-scale), image-driven layout
import tkinter as tk
from math import cos, sin, radians, atan2, degrees
import re
import time
import os

# === Configuration ===
BACKGROUND_FILENAME = "radio_UI.png"        # base PNG skin, rendered at half-scale
KNOB_FILENAME = "radio_UI_knob.png"         # alternate PNG used to "animate" the channel knob
TRANSPARENT_KEY = "#11ff11"
KNOB_ANIM_MS = 70                           # milliseconds between animation frames

# Individually adjustable per-channel RX lamp rectangles (x0, y0, x1, y1)
# Defaults place them under the display, but tweak these to match your PNG labels exactly.
CHAN_LAMP_COORDS = {
    'A': (27, 442, 36, 451),   # <-- edit these four
    'B': (27, 467, 36, 476),   # positions to your
    'C': (27, 492, 36, 501),   # exact PNG layout
    'D': (27, 517, 36, 526),
}

# === Keypad configuration (fully adjustable) ===
KEYPAD_ENABLED = True
KEYPAD_X = 92        # keypad origin X (half-scale coordinates)
KEYPAD_Y = 425       # keypad origin Y
KEYPAD_BTN_W = 34    # button width
KEYPAD_BTN_H = 35    # button height
KEYPAD_GAP_X = 2     # horizontal spacing
KEYPAD_GAP_Y = 2     # vertical spacing
KEYPAD_FONT = ("Consolas", 12, "bold")
KEYPAD_RADIUS = 6
KEYPAD_ENTER_LABEL = "ENTER"

def _resolve_asset_path(filename: str) -> str:
    """
    Resolves PNG paths with a preference for the new app/assets/ directory.
    Tries (in order):
        <this_dir>/assets/<filename>
        <cwd>/app/assets/<filename>
        <cwd>/assets/<filename>
        <this_dir>/<filename>
        <cwd>/<filename>
    Returns the best guess path (even if it doesn't exist, to let Tk raise helpful errors).
    """
    here = os.path.dirname(__file__)
    candidates = [
        os.path.join(here, "assets", filename),
        os.path.join(os.getcwd(), "app", "assets", filename),
        os.path.join(os.getcwd(), "assets", filename),
        os.path.join(here, filename),
        os.path.join(os.getcwd(), filename),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]

# -----------------------------
# Seven-segment renderer
# -----------------------------
SEG_MAP = {
    "0": ("a","b","c","d","e","f"),
    "1": ("b","c"),
    "2": ("a","b","g","e","d"),
    "3": ("a","b","g","c","d"),
    "4": ("f","g","b","c"),
    "5": ("a","f","g","c","d"),
    "6": ("a","f","g","e","c","d"),
    "7": ("a","b","c"),
    "8": ("a","b","c","d","e","f","g"),
    "9": ("a","b","c","d","f","g"),
    "-": ("g",),
    " ": tuple(),
    "A": ("a","b","c","e","f","g"),
    "B": ("c","d","e","f","g"),
    "C": ("a","d","e","f"),
    "D": ("b","c","d","e","g"),
}

class SevenSeg:
    def __init__(self, canvas, x, y, w=18, h=32, seg_w=4, fg="#9FFFA0"):
        self.canvas = canvas
        self.x = x; self.y = y
        self.w = w; self.h = h
        self.seg_w = seg_w
        self.fg = fg
        self.items = {"a":None,"b":None,"c":None,"d":None,"e":None,"f":None,"g":None,"p":None}

    def _coords(self):
        x, y, w, h, s = self.x, self.y, self.w, self.h, self.seg_w
        return {
            "a": (x+s, y, x+w-s, y+s),
            "g": (x+s, y+(h//2)-s//2, x+w-s, y+(h//2)+s//2),
            "d": (x+s, y+h-s, x+w-s, y+h),
            "b": (x+w-s, y+s, x+w, y+(h//2)-s//2),
            "c": (x+w-s, y+(h//2)+s//2, x+w, y+h-s),
            "e": (x, y+(h//2)+s//2, x+s, y+h-s),
            "f": (x, y+s, x+s, y+(h//2)-s//2),
            "p": (x+w+2, y+h-6, x+w+5, y+h-3),
        }

    def draw_char(self, ch, dot=False):
        ch = (ch or " ").upper()
        coords = self._coords()
        lit = set(SEG_MAP.get(ch, tuple()))
        for seg in ("a","b","c","d","e","f","g"):
            xy = coords[seg]
            color = self.fg if seg in lit else "#243424"
            if self.items[seg] is None:
                self.items[seg] = self.canvas.create_rectangle(*xy, fill=color, outline="")
            else:
                self.canvas.coords(self.items[seg], *xy)
                self.canvas.itemconfig(self.items[seg], fill=color)
        pxy = self._coords()["p"]
        pcolor = self.fg if dot else "#0a140a"
        if self.items["p"] is None:
            self.items["p"] = self.canvas.create_oval(*pxy, fill=pcolor, outline="")
        else:
            self.canvas.coords(self.items["p"], *pxy)
            self.canvas.itemconfig(self.items["p"], fill=pcolor)

# -----------------------------
# Knob widget
# -----------------------------
class Knob:
    def __init__(self, canvas, cx, cy, r=22, label="VOL", on_change=None,
                 min_val=0, max_val=100, step=10, initial=100, format_value=None,
                 ind_dx=0, ind_dy=0, ind_inner_offset=10, ind_outer_offset=14, ind_angle_deg=0,
                 ind_color="#BABABA", pointer_color="#BABABA"):
        self.canvas = canvas
        self.cx = cx; self.cy = cy; self.r = r
        self.label = label
        self.on_change = on_change
        self.min_val = min_val; self.max_val = max_val; self.step = step
        self.value = self._snap(initial)
        self.dragging = False
        self.ids = {}
        self.tag = f"knob_{id(self)}"
        self._hovering = False

        self.ind_dx = ind_dx; self.ind_dy = ind_dy
        self.ind_inner_offset = ind_inner_offset
        self.ind_outer_offset = ind_outer_offset
        self.ind_angle_deg = ind_angle_deg
        self.ind_color = ind_color
        self.pointer_color = pointer_color
        self.format_value = format_value

        self._draw()
        for tag in (self.tag, "label_"+self.tag):
            canvas.tag_bind(tag, "<Enter>", self._hover_on)
            canvas.tag_bind(tag, "<Leave>", self._hover_off)
            canvas.tag_bind(tag, "<Button-1>", self._start_drag)
            canvas.tag_bind(tag, "<B1-Motion>", self._drag)
            canvas.tag_bind(tag, "<ButtonRelease-1>", self._stop_drag)

    def _snap(self, v):
        v = max(self.min_val, min(self.max_val, int(v)))
        return int(round(v / float(self.step))) * self.step

    def _angle_for_value(self, v):
        span = max(1, self.max_val - self.min_val)
        t = (v - self.min_val) / float(span)
        return -135 + 270 * t

    def _value_for_angle(self, deg):
        d = max(-135.0, min(135.0, deg))
        t = (d + 135.0) / 270.0
        return self.min_val + t * (self.max_val - self.min_val)

    def _draw(self):
        c = self.canvas; cx, cy, r = self.cx, self.cy, self.r
        self.ids["bezel"]  = c.create_oval(cx-r-5, cy-r-5, cx+r+5, cy+r+5, outline="", width=2, fill="", tags=(self.tag,))
        self.ids["body"]   = c.create_oval(cx-r, cy-r, cx+r, cy+r, outline="", width=2, fill="", tags=(self.tag,))

        self.ids["ticks"] = []
        for i in range(12):
            ang = radians(i*30 + self.ind_angle_deg)
            icx = cx + self.ind_dx; icy = cy + self.ind_dy
            x1 = icx + cos(ang)*(r + self.ind_outer_offset)
            y1 = icy + sin(ang)*(r + self.ind_outer_offset)
            x2 = icx + cos(ang)*(r + self.ind_inner_offset)
            y2 = icy + sin(ang)*(r + self.ind_inner_offset)
            line_id = c.create_line(x1, y1, x2, y2, width=1, fill=self.ind_color, tags=(self.tag,))
            self.ids["ticks"].append(line_id)

        theta = radians(self._angle_for_value(self.value))
        px = cx + cos(theta)*(r-12); py = cy + sin(theta)*(r-12)
        self.ids["pointer"] = c.create_line(cx, cy, px, py, fill=self.pointer_color, width=3, capstyle="round", tags=(self.tag,))
        self.ids["label"] = c.create_text(cx, cy+r+14, text=self.label, fill="#8EEA6A", font=("Consolas", 9, "bold"), tags=("label_"+self.tag,))

    def _update_pointer(self):
        cx, cy, r = self.cx, self.cy, self.r
        theta = radians(self._angle_for_value(self.value))
        px = cx + cos(theta)*(r-12); py = cy + sin(theta)*(r-12)
        self.canvas.coords(self.ids["pointer"], cx, cy, px, py)

    def _hover_on(self, _): self._hovering = True; self.canvas.config(cursor="hand2")
    def _hover_off(self, _): self._hovering = False
    def _start_drag(self, _): self.dragging = True; self.canvas.config(cursor="hand2")
    def _drag(self, e):
        if not self.dragging: return
        cx, cy = self.cx, self.cy
        dx = e.x - cx; dy = e.y - cy
        ang = degrees(atan2(dy, dx))
        up_ref = ang + 90.0
        while up_ref <= -180: up_ref += 360
        while up_ref > 180: up_ref -= 360
        new_val = self._snap(self._value_for_angle(up_ref))
        if new_val != self.value:
            self.value = new_val; self._update_pointer()
            if callable(self.on_change): self.on_change(self.value)
    def _stop_drag(self, _): self.dragging = False; self.canvas.config(cursor="")

    def wheel_step(self, direction):
        new_val = self._snap(self.value + direction * self.step)
        if new_val != self.value:
            self.value = new_val; self._update_pointer()
            if callable(self.on_change): self.on_change(self.value)

    def hit_test(self, x, y, pad=5):
        dx = x - self.cx; dy = y - self.cy
        return (dx*dx + dy*dy) <= (self.r + pad) ** 2

# -----------------------------
# Overlay window
# -----------------------------
class OverlayWindow:
    def __init__(self, app, open_immediately=True):
        self.app = app
        self.root = app.root
        self.win = None
        self.opacity = 0.98
        self.lock_pos = False
        self._poll_ms = 100
        self._rx_cached_list = []
        self._rx_last_ts = 0.0

        self._seg_chars = []
        self._freq_editor = None
        self._freq_click_after = None

        # Live keypad buffer (renders directly to seven-seg, with leading zeros)
        self._keypad_buffer = None      # e.g., "000.0"
        self._freq_keypad_mode = False
        self._freq_edit_pos = 0         # next position index in [0,1,2,4]

        # Background images (half-scale)
        self._bg_img_base = None        # PhotoImage
        self._bg_img_knob = None        # PhotoImage
        self._bg_img_current = None     # PhotoImage currently displayed
        self._bg_item = None
        self._bg_w = 232
        self._bg_h = 604

        # Keypad draw items
        self._keypad_items = []

        # Animation state
        self._knob_anim_after = None

        # Drag state (throttled to avoid stutter when moving the window)
        self._drag_off = (0, 0)
        self._drag_last_ms = 0
        self._drag_last_xy = None
        self._drag_throttle_ms = 16

        if open_immediately:
            self.open()

    # ---------- build ----------
    def open(self):
        if self.win is not None:
            try:
                self.win.deiconify(); self.win.lift()
            except Exception: pass
            return

        self.win = tk.Toplevel(self.root)
        self.win.title("Interstellar Com Radio — Overlay")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=TRANSPARENT_KEY)
        try: self.win.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception: pass
        try: self.win.attributes("-alpha", self.opacity)
        except Exception: pass

        self._load_background_halfscale()

        self.canvas = tk.Canvas(self.win, width=self._bg_w, height=self._bg_h, bg=TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        if self._bg_img_current is not None:
            self._bg_item = self.canvas.create_image(0, 0, image=self._bg_img_current, anchor="nw")
            self.canvas.tag_bind(self._bg_item, "<Button-1>", self._start_drag)
            self.canvas.tag_bind(self._bg_item, "<B1-Motion>", self._on_drag)

        self.drag_bar = self.canvas.create_rectangle(0, 0, self._bg_w, 26, outline="", fill="")
        self.canvas.tag_bind(self.drag_bar, "<Button-1>", self._start_drag)
        self.canvas.tag_bind(self.drag_bar, "<B1-Motion>", self._on_drag)

        self.menu = tk.Menu(self.win, tearoff=0)
        self._lock_var = tk.BooleanVar(value=self.lock_pos)
        self.menu.add_checkbutton(label="Lock position", variable=self._lock_var, command=self._toggle_lock)
        for pct in (100, 98, 92, 85, 75):
            self.menu.add_command(label=f"Opacity {pct}%", command=lambda p=pct: self._set_opacity(p/100.0))
        self.menu.add_separator(); self.menu.add_command(label="Hide (F10)", command=self.hide)
        self.menu.add_command(label="Close Overlay", command=self.close)
        menu_hot = self.canvas.create_text(self._bg_w-6, 12, text="⋮", anchor="e", fill="#e4f1df", font=("Segoe UI Symbol", 12))
        self.canvas.tag_bind(menu_hot, "<Button-1>", self._open_menu_at_mouse)

        self._draw_display()
        self._draw_lamps()
        self._draw_scan_toggle()

        self.knob_vol = Knob(self.canvas, cx=172, cy=267, r=20, label="",
                             on_change=self._on_knob_vol, min_val=0, max_val=100, step=10,
                             initial=self._get_active_vol(),
                             ind_dx=2, ind_dy=-3, ind_inner_offset=10, ind_outer_offset=14, ind_angle_deg=0,
                             ind_color="#BABABA", pointer_color="#BABABA",
                             format_value=lambda v: f"VOL {int(v)}%")
        self.knob_chan = Knob(self.canvas, cx=175, cy=178, r=20, label="",
                              on_change=self._on_knob_chan, min_val=0, max_val=100, step=33,
                              initial=self._chan_to_val(self._get_active_chan_safe()),
                              ind_dx=0, ind_dy=0, ind_inner_offset=10, ind_outer_offset=14, ind_angle_deg=0,
                              ind_color="#BABABA", pointer_color="#BABABA",
                              format_value=lambda v: f"CHAN {['A','B','C','D'][0 if v<17 else (1 if v<50 else (2 if v<83 else 3))]}")

        self.canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_canvas_wheel(e, +1))
        self.canvas.bind("<Button-5>", lambda e: self._on_canvas_wheel(e, -1))

        if KEYPAD_ENABLED:
            self._draw_keypad(KEYPAD_X, KEYPAD_Y, KEYPAD_BTN_W, KEYPAD_BTN_H, KEYPAD_GAP_X, KEYPAD_GAP_Y, KEYPAD_FONT, KEYPAD_RADIUS)
        # --- PTT mode button + lamp (hidden text & hidden bezel, hotspot only) ---
        self._draw_ptt_mode_switch()
        try:
            self.app.ptt_mode.trace_add('write', lambda *a: self._update_ptt_mode_visual())
        except Exception:
            pass


        self._tick()

    def _load_background_halfscale(self):
        """Load both base and knob PNGs at half scale and pick base as initial."""
        base_path = _resolve_asset_path(BACKGROUND_FILENAME)
        knob_path = _resolve_asset_path(KNOB_FILENAME)
        self._bg_img_base = None
        self._bg_img_knob = None
        self._bg_img_current = None
        try:
            self._bg_img_base = tk.PhotoImage(file=base_path).subsample(2, 2)  # half-scale
            self._bg_w = self._bg_img_base.width(); self._bg_h = self._bg_img_base.height()
        except Exception:
            self._bg_img_base = None
            self._bg_w, self._bg_h = 232, 604
        try:
            self._bg_img_knob = tk.PhotoImage(file=knob_path).subsample(2, 2)
        except Exception:
            self._bg_img_knob = None
        # Start with base if available, else knob, else None
        self._bg_img_current = self._bg_img_base or self._bg_img_knob

    def _set_background_image(self, img):
        """Swap the canvas background image (keep a reference to prevent GC)."""
        if self._bg_item is None or img is None:
            self._bg_img_current = img
            return
        self._bg_img_current = img
        try:
            self.canvas.itemconfig(self._bg_item, image=self._bg_img_current)
        except Exception:
            pass

    # -------------------- Display --------------------
    def _draw_display(self):
        c = self.canvas
        self.display_box = (28, 319, 164, 365)
        x0, y0, x1, y1 = self.display_box

        self._display_hit = c.create_rectangle(x0, y0, x1, y1, outline="", fill="", tags=("freq_hit",))
        c.tag_bind(self._display_hit, "<Button-1>", self._freq_click)
        c.tag_bind(self._display_hit, "<Double-Button-1>", lambda e: (self._cancel_freq_click_timer(), self._open_freq_editor(e)))

        x = x0 + 10
        chars = ["A"," ", "1","0","2",".","3"]
        self._seg_chars = []
        for ch in chars:
            if ch == ".":
                if self._seg_chars:
                    self._seg_chars[-1][1] = True
                continue
            seg = SevenSeg(c, x, y0+6, w=14, h=24, seg_w=3)
            self._seg_chars.append([seg, False, ch])
            x += 23

        self.label_freq = c.create_text(x0+10, y1+0, text="FREQ", anchor="w", fill="#9FFFA0", font=("Consolas", 8))
        self.label_vol  = c.create_text(x0+60, y1+0, text="VOL 100%", anchor="w", fill="#9FFFA0", font=("Consolas", 8))

        self.scan_text = c.create_text(x1 - 90, y0 + 60, text="", anchor="ne", fill="#9FFFA0", font=("Consolas", 8, "bold"))

        
                # Per-channel RX lamps (A, B, C, D) under the display
        # No text labels; PNG already has channel letters.
        # Lamps use coordinates from CHAN_LAMP_COORDS and can be edited individually.
        self.chan_lamps = []  # list of oval_ids in A,B,C,D order
        order = ['A','B','C','D']
        for letter in order:
            try:
                lx0, ly0, lx1, ly1 = CHAN_LAMP_COORDS[letter]
            except Exception:
                # Fallback layout based on the display box if config missing/corrupt
                idx = order.index(letter)
                lamp_y0 = y1 + 22
                lamp_y1 = lamp_y0 + 9
                lamp_x = x0 + 10 + idx * 24
                lx0, ly0, lx1, ly1 = lamp_x, lamp_y0, lamp_x + 9, lamp_y1
            oval = c.create_oval(lx0, ly0, lx1, ly1, fill="#0a1a0a", outline="#123312")
            self.chan_lamps.append(oval)


    def _draw_lamps(self):
        c = self.canvas
        # TX lamp (red)
        self.tx_lamp = c.create_oval(42, 239, 51, 248, fill="#1a0a0a", outline="#331212")
        # RX lamp (green), placed to the right of TX
        self.rx_lamp = c.create_oval(42, 257, 51, 266, fill="#0a1a0a", outline="#123312")
        # Signal quality indicator bar
        self.sqi_back = c.create_rectangle(220, 322, 242, 332, fill="#0a141a", outline="#12202a")
        self.sqi_bar = c.create_rectangle(220, 322, 220, 332, fill="#6AC4FF", outline="")

    def _draw_scan_toggle(self):
        c = self.canvas
        x0, y0 = 34, 407
        x1, y1 = x0 + 56, y0 + 22
        self.scan_btn_bg = c.create_rectangle(x0, y0, x1, y1, fill="", outline="", width=2)
        self.scan_btn_txt = c.create_text((x0 + x1)//2, (y0 + y1)//2, text="", fill="", font=("Consolas", 9, "bold"))
        c.tag_bind(self.scan_btn_bg, "<Button-1>", self._on_scan_toggle)
        c.tag_bind(self.scan_btn_txt, "<Button-1>", self._on_scan_toggle)
        self._update_scan_visual()

    # -------------------- Keypad --------------------
    def _rounded_rect(self, x0, y0, x1, y1, r=6, **kw):
        r = max(0, min(r, int(min(x1-x0, y1-y0)/2)))
        parts = []
        parts.append(self.canvas.create_rectangle(x0+r, y0, x1-r, y1, **kw))
        parts.append(self.canvas.create_rectangle(x0, y0+r, x1, y1-r, **kw))
        parts.append(self.canvas.create_oval(x0, y0, x0+2*r, y0+2*r, **kw))
        parts.append(self.canvas.create_oval(x1-2*r, y0, x1, y0+2*r, **kw))
        parts.append(self.canvas.create_oval(x0, y1-2*r, x0+2*r, y1, **kw))
        parts.append(self.canvas.create_oval(x1-2*r, y1-2*r, x1, y1, **kw))
        return parts

    def _draw_keypad(self, ox, oy, bw, bh, gx, gy, font, rad):
        labels = [
            ["1","2","3"],
            ["4","5","6"],
            ["7","8","9"],
            ["0", KEYPAD_ENTER_LABEL],
        ]
        self._keypad_items.clear()
        for row_i, row in enumerate(labels):
            for col_i, label in enumerate(row):
                x0 = ox + col_i * (bw + gx)
                y0 = oy + row_i * (bh + gy)
                w = bw
                if row_i == 3 and label == KEYPAD_ENTER_LABEL:
                    x0 = ox + 1 * (bw + gx)
                    w = bw * 2 + gx
                x1 = x0 + w
                y1 = y0 + bh

                parts = self._rounded_rect(x0, y0, x1, y1, r=rad, fill="", outline="")
                tag = f"key_{label}_{row_i}_{col_i}"
                hit = self.canvas.create_rectangle(x0, y0, x1, y1, outline="", fill="", tags=(tag,))
                txt = self.canvas.create_text((x0+x1)//2, (y0+y1)//2, text=label, font=font, fill="#d3e9cf", tags=(tag,), state="hidden")

                if label == KEYPAD_ENTER_LABEL:
                    self.canvas.tag_bind(tag, "<Button-1>", lambda e: self._on_keypad_enter())
                else:
                    self.canvas.tag_bind(tag, "<Button-1>", lambda e, ch=label: self._on_keypad_digit(ch))

                self._keypad_items.append((parts, txt, label, (x0,y0,x1,y1), hit, tag))

        self.canvas.create_text(ox, oy-12, text="", anchor="w", fill="#9FFFA0", font=("Consolas", 8, "bold"))

    # === Keypad handlers: fill order [0,1,2,4] with leading zeros ===
    def _on_keypad_digit(self, ch):
        if not self._freq_keypad_mode or not isinstance(self._keypad_buffer, str):
            self._freq_keypad_mode = True
            self._keypad_buffer = "000.0"   # fixed length (ddd.d) with leading zeros
            self._freq_edit_pos = 0         # next index in positions

        positions = [0, 1, 2, 4]  # hundreds, tens, ones, tenths
        buf = list(self._keypad_buffer)
        if self._freq_edit_pos < len(positions):
            idx = positions[self._freq_edit_pos]
            buf[idx] = ch
            self._freq_edit_pos += 1
            # keep the decimal at index 3
            if buf[3] != ".":
                buf[3] = "."
            self._keypad_buffer = "".join(buf)

        try:
            self.app.sounds.play_switch()
        except Exception:
            pass

    def _on_keypad_enter(self):
        try:
            self.app.sounds.play_switch()
        except Exception:
            pass

        buf = self._keypad_buffer or ""
        if re.fullmatch(r"\d{3}\.\d", buf):
            try:
                idx = int(self.app.active_chan.get())
            except Exception:
                idx = 0
            if 0 <= idx < 3:
                try:
                    self.app.chan_vars[idx].set(buf)
                    try: self.app._save_user_config_all()
                    except Exception: pass
                except Exception:
                    pass

        self._freq_keypad_mode = False
        self._keypad_buffer = None
        self._freq_edit_pos = 0

    # -------------------- Misc controls --------------------
    def _open_menu_at_mouse(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _start_drag(self, e):
        if self.lock_pos: return
        self._drag_off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())
        self._drag_last_ms = 0
        self._drag_last_xy = None

    def _on_drag(self, e):
        if self.lock_pos: return
        # Allow negative coordinates so the overlay can move to any monitor
        x = e.x_root - self._drag_off[0]
        y = e.y_root - self._drag_off[1]
        xi, yi = int(x), int(y)
        now_ms = int(time.time() * 1000)
        if self._drag_last_xy == (xi, yi):
            return
        if now_ms - self._drag_last_ms < self._drag_throttle_ms:
            return
        self._drag_last_ms = now_ms
        self._drag_last_xy = (xi, yi)
        self.win.geometry(f"+{xi}+{yi}")

    def _toggle_lock(self):
        self.lock_pos = not self.lock_pos
        self._lock_var.set(self.lock_pos)

    def _set_opacity(self, a):
        self.opacity = max(0.3, min(1.0, float(a)))
        try: self.win.attributes("-alpha", self.opacity)
        except Exception: pass

    def hide(self):
        if self.win is not None:
            try: self.win.withdraw()
            except Exception: pass

    def close(self):
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass
        self.win = None

    def toggle(self):
        if self.win is None:
            self.open(); return
        try:
            if self.win.state() == "withdrawn":
                self.win.deiconify(); self.win.lift()
            else:
                self.hide()
        except Exception:
            self.open()

    # ---------- knob handlers ----------
    def _get_active_vol(self):
        try:
            idx = int(self.app.active_chan.get())
        except Exception:
            idx = 0

        # Use the stored 0-100 slider value so UI and app stay on the same scale.
        try:
            if 0 <= idx < 3:
                return max(0, min(100, int(self.app.chan_vol_vars[idx].get())))
            elif idx == 3:
                return max(0, min(100, int(getattr(self.app, "chan_d_vol_var").get())))
        except Exception:
            pass

        # Fallback: if only the gain multiplier is available (0.0-2.0), map it back to 0-100%.
        try:
            fn = getattr(self.app, "get_channel_volume", None)
            if callable(fn):
                gain = max(0.0, min(2.0, float(fn(idx))))
                return int(round(gain * 50.0))  # 2.0x gain == 100% on the knob
        except Exception:
            pass
        return 100

    def _on_knob_vol(self, val):
        try:
            delta = int(val) - int(self._get_active_vol())
            if delta != 0: self.app._bump_active_volume(delta)
        except Exception:
            pass

    def _chan_to_val(self, idx): return max(0, min(100, int(idx) * 33))
    def _val_to_chan(self, val):
        if val < 17: return 0
        elif val < 50: return 1
        elif val < 83: return 2
        return 3

    def _animate_knob_turn(self, prev_idx, new_idx):
        """Alternate between base and knob PNGs based on the *current* image.
        - First frame: flip to the opposite of whatever is currently shown.
        - Total frames = steps (distance in channels).
        - Resulting final image reflects parity: odd steps end on opposite, even steps end where we started.
        This guarantees a visible flip for every change, even for repeated single-step hops."""
        if self._bg_item is None: return
        if self._bg_img_base is None or self._bg_img_knob is None: return

        steps = max(1, abs(int(new_idx) - int(prev_idx)))
        current = self._bg_img_current
        if current is None:
            current = self._bg_img_base

        # Determine the 'opposite' image from the current one
        opposite = self._bg_img_knob if current is self._bg_img_base else self._bg_img_base

        # Build a sequence that starts with the opposite, then alternates for `steps` frames
        sequence = []
        next_img = opposite
        for _ in range(steps):
            sequence.append(next_img)
            next_img = self._bg_img_base if next_img is self._bg_img_knob else self._bg_img_knob

        # Cancel any prior animation
        if self._knob_anim_after is not None:
            try: self.win.after_cancel(self._knob_anim_after)
            except Exception: pass
            self._knob_anim_after = None

        def _run(seq, i=0):
            if i >= len(seq):
                # Do not force a reset; final frame remains visible by design
                return
            self._set_background_image(seq[i])
            self._knob_anim_after = self.win.after(KNOB_ANIM_MS, lambda: _run(seq, i+1))

        _run(sequence, 0)

    def _on_knob_chan(self, val):
        try:
            target = self._val_to_chan(val)
            cur = int(self.app.active_chan.get())
            if target != cur:
                # Trigger the background animation before/around state change.
                self._animate_knob_turn(cur, target)
                self.app.active_chan.set(target)
                self.app._after_channel_change()
        except Exception:
            pass

    # ---------- wheel routing ----------
    def _active_knob_for_wheel(self, event=None):
        for k in (self.knob_vol, self.knob_chan):
            if getattr(k, "dragging", False): return k
        for k in (self.knob_vol, self.knob_chan):
            if getattr(k, "_hovering", False): return k
        if event is not None:
            x, y = event.x, event.y
            for k in (self.knob_vol, self.knob_chan):
                if k.hit_test(x, y): return k
        return None

    def _on_canvas_wheel(self, event, direction=None):
        knob = self._active_knob_for_wheel(event)
        if knob is None: return
        if direction is None:
            try: direction = 1 if event.delta > 0 else -1
            except Exception: direction = 0
        if direction == 0: return
        knob.wheel_step(direction)

    # ---------- freq editor (manual editing via LCD) ----------
    def _freq_click(self, _=None):
        self._cancel_freq_click_timer()
        try: self._freq_click_after = self.win.after(220, lambda: self._open_freq_editor(None))
        except Exception: pass

    def _cancel_freq_click_timer(self):
        if getattr(self, "_freq_click_after", None):
            try: self.win.after_cancel(self._freq_click_after)
            except Exception: pass
        self._freq_click_after = None

    def _open_freq_editor(self, _=None):
        self._cancel_freq_click_timer()
        if self._freq_editor is not None or self.win is None: return
        try: idx = int(self.app.active_chan.get())
        except Exception: idx = 0
        if idx == 3: return

        try: cur = self.app.chan_vars[idx].get() or ""
        except Exception: cur = ""

        self._freq_editor = tk.Entry(self.win, font=("Consolas", 16, "bold"),
                                     bg="#0b1410", fg="#9FFFA0",
                                     insertbackground="#9FFFA0",
                                     relief="flat", justify="center",
                                     validate="key")
        vcmd = self.win.register(self._validate_freq_entry)
        self._freq_editor.configure(validatecommand=(vcmd, "%P"))

        text = self._normalize_freq_text(cur) or cur or ""
        self._freq_editor.insert(0, text)

        x0, y0, x1, y1 = self.display_box
        self._freq_editor.place(x=x0+4, y=y0+4, width=(x1-x0-8), height=(y1-y0-8))

        self._freq_editor.bind("<Return>", self._apply_freq_from_editor)
        self._freq_editor.bind("<KP_Enter>", self._apply_freq_from_editor)
        self._freq_editor.bind("<Escape>", lambda e: self._close_freq_editor())
        self._freq_editor.bind("<KeyRelease>", self._freq_editor_keyrelease)
        self._freq_editor.focus_set(); self._freq_editor.select_range(0, "end")
        self.win.bind("<Button-1>", self._cancel_if_click_outside, add="+")

        # Opening manual editor cancels keypad mode
        self._freq_keypad_mode = False
        self._keypad_buffer = None
        self._freq_edit_pos = 0

    def _validate_freq_entry(self, proposed: str):
        s = (proposed or "").strip()
        if s == "": return True
        if not re.fullmatch(r"[0-9.]*", s): return False
        if "." in s:
            if not re.match(r"^\d{3}", s): return False
            if len(s) >= 4 and s[3] != ".": return False
            if not re.fullmatch(r"^\d{3}\.?\d?$", s): return False
            if len(s.replace(".", "")) > 4: return False
            return True
        else:
            return bool(re.fullmatch(r"^\d{0,3}$", s))

    def _freq_editor_keyrelease(self, _=None):
        if self._freq_editor is None: return
        if self._freq_keypad_mode: return
        try:
            text = self._freq_editor.get()
            digits = text.replace(".", "")
            if len(digits) == 3 and "." not in text:
                self._freq_editor.delete(0, "end")
                self._freq_editor.insert(0, digits + ".")
                self._freq_editor.icursor("end")
        except Exception: pass

    def _cancel_if_click_outside(self, event):
        if self._freq_editor is None: return
        try:
            ex, ey = event.x, event.y
            info = self._freq_editor.place_info()
            x = int(info.get("x", 0)); y = int(info.get("y", 0))
            w = int(info.get("width", 0)); h = int(info.get("height", 0))
            if not (x <= ex <= x+w and y <= ey <= y+h):
                self._close_freq_editor()
        except Exception:
            self._close_freq_editor()

    def _normalize_freq_text(self, s):
        s = (s or "").strip()
        if re.fullmatch(r"\d{3}\.\d", s): return s
        digits = re.sub(r"[^0-9]", "", s)
        if len(digits) >= 4: return f"{digits[0:3]}.{digits[3]}"
        return None

    def _apply_freq_from_editor(self, _=None):
        if self._freq_editor is None: return
        raw = (self._freq_editor.get() or "").strip()
        if not re.fullmatch(r"\d{3}\.\d", raw):
            norm = self._normalize_freq_text(raw)
            if norm is None:
                try: self._freq_editor.configure(bg="#3b1212"); self.win.after(180, lambda: self._freq_editor.configure(bg="#0b1410"))
                except Exception: pass
                return
            raw = norm
        try:
            idx = int(self.app.active_chan.get())
            if 0 <= idx < 3:
                self.app.chan_vars[idx].set(raw)
                try: self.app._save_user_config_all()
                except Exception: pass
                try: self.app.sounds.play_switch()
                except Exception: pass
        except Exception: pass
        finally:
            self._close_freq_editor()
            self._freq_keypad_mode = False
            self._keypad_buffer = None
            self._freq_edit_pos = 0

    def _close_freq_editor(self):
        try:
            if self._freq_editor is not None:
                self._freq_editor.place_forget(); self._freq_editor.destroy()
        except Exception: pass
        self._freq_editor = None
        try: self.win.unbind("<Button-1>")
        except Exception: pass

    # ---------- update loop ----------
    def _tick(self):
        if self.win is None: return
        try:
            idx = self._get_active_chan_safe()
            name = ["A","B","C","D"][idx] if 0 <= idx < 4 else " "
            try:
                if idx < 3:
                    if self._freq_keypad_mode and isinstance(self._keypad_buffer, str) and self._keypad_buffer:
                        freq = self._keypad_buffer
                    else:
                        freq = self.app.chan_vars[idx].get() or "000.0"  # show leading zeros by default
                else:
                    freq = getattr(self.app, "chan_d_var").get() or "111.1"
            except Exception:
                freq = "000.0"
            seq = [name, " "]
            if "." in freq and len(freq) >= 5:
                digits = [freq[0], freq[1], freq[2]]; dec = freq[4:5] or "0"
            else:
                f = (freq + "     ")[:5]
                digits = [f[0], f[1], f[2]]; dec = "0"
            seq.extend(digits); seq.append("."); seq.append(dec)

            si = 0
            for ch in seq:
                if ch == ".":
                    if si > 0:
                        seg, _, prev_char = self._seg_chars[si-1]
                        seg.draw_char(prev_char, dot=True)
                    continue
            si = 0
            for ch in seq:
                if ch == ".":
                    if si > 0:
                        seg, _, prev_char = self._seg_chars[si-1]
                        seg.draw_char(prev_char, dot=True)
                    continue
                seg, dot, _ = self._seg_chars[si]
                self._seg_chars[si][2] = ch
                seg.draw_char(ch, dot=False)
                si += 1

            vol = int(self._get_active_vol())
            self.canvas.itemconfig(self.label_vol, text=f"VOL {vol}%")

            if bool(self.app.ptt.get()):
                self.canvas.itemconfig(self.tx_lamp, fill="#d43c3c", outline="#5c1515")
            else:
                self.canvas.itemconfig(self.tx_lamp, fill="#1a0a0a", outline="#331212")

            # Build current RX set once so both global + per-channel lamps stay in sync
            rx_set = set()
            try:
                rx_fn = getattr(self.app, "get_active_rx_channels", None)
                raw = rx_fn() or [] if callable(rx_fn) else []
                if not raw:
                    rx_fn2 = getattr(self.app, "get_rx_active_channels", None)
                    raw = rx_fn2() or [] if callable(rx_fn2) else []
                for i_f in raw:
                    try:
                        # Accept (idx, freq) tuples or plain indexes
                        if isinstance(i_f, (list, tuple)) and len(i_f) >= 1:
                            rx_set.add(int(i_f[0]))
                        else:
                            rx_set.add(int(i_f))
                    except Exception:
                        pass
            except Exception:
                pass

            # RX lamp (global): light if rx_set OR recent audio (timestamps/queue)
            recent_rx = False
            try:
                import time as _time
                now_t = _time.time()
                recent_rx = (now_t - float(getattr(self.app, 'rx_active_recent_ts', 0.0))) < 2.0
            except Exception:
                recent_rx = False
            queued_rx = False
            try:
                q = getattr(self.app, "_rx_queue", None)
                if q is not None:
                    queued_rx = len(q) > 0
            except Exception:
                queued_rx = False

            if rx_set or recent_rx or queued_rx:
                self.canvas.itemconfig(self.rx_lamp, fill="#3cd45a", outline="#165c24")
            else:
                self.canvas.itemconfig(self.rx_lamp, fill="#0a1a0a", outline="#123312")

            x0, y0, x1, y1 = self.canvas.coords(self.sqi_back)
            width = x1 - x0
            try:
                sqi_var = getattr(self.app, "sqi", None)
                sqi_val = float(sqi_var.get()) if sqi_var is not None else 0.0
            except Exception:
                try:
                    sqi_val = float(getattr(self.app, "signal_quality", 0.0))
                except Exception:
                    sqi_val = 0.0
            sqi = max(0.0, min(1.0, sqi_val))
            self.canvas.coords(self.sqi_bar, x0, y0, x0 + int(width * sqi), y1)

            self.knob_vol.value = vol; self.knob_vol._update_pointer()
            self.knob_chan.value = self._chan_to_val(idx); self.knob_chan._update_pointer()

            self._update_scan_visual()


            # Per-channel RX lamps update
            # Light each lamp green if its channel is receiving
            for i in range(4):
                try:
                    oval_id = self.chan_lamps[i]
                    if i in rx_set:
                        self.canvas.itemconfig(oval_id, fill="#3cd45a", outline="#165c24")
                    else:
                        self.canvas.itemconfig(oval_id, fill="#0a1a0a", outline="#123312")
                except Exception:
                    pass

        except Exception: pass
        self.win.after(self._poll_ms, self._tick)

    def _get_active_chan_safe(self):
        try: return int(self.app.active_chan.get())
        except Exception: return 0

    def _get_scan_state(self):
        idx = self._get_active_chan_safe()
        if idx == 3: return True
        for attr in ("chan_scan_vars", "scan_vars", "scan_enabled_vars"):
            try:
                arr = getattr(self.app, attr, None)
                if arr is not None and 0 <= idx < len(arr):
                    return bool(arr[idx].get())
            except Exception: pass
        fn = getattr(self.app, "is_scan_enabled", None)
        if callable(fn):
            try: return bool(fn(idx))
            except Exception: pass
        return False

    def _set_scan_state(self, state):
        idx = self._get_active_chan_safe()
        if idx == 3: return
        for attr in ("chan_scan_vars", "scan_vars", "scan_enabled_vars"):
            try:
                arr = getattr(self.app, attr, None)
                if arr is not None and 0 <= idx < len(arr):
                    arr[idx].set(1 if state else 0)
                    try: self.app._save_user_config_all()
                    except Exception: pass
                    try: self.app.sounds.play_switch()
                    except Exception: pass
                    try: self.app._notify_server()
                    except Exception: pass
                    return
            except Exception: pass
        setter = getattr(self.app, "set_scan_enabled", None)
        if callable(setter):
            try:
                setter(idx, bool(state))
                try: self.app._save_user_config_all()
                except Exception: pass
                try: self.app.sounds.play_switch()
                except Exception: pass
                try: self.app._notify_server()
                except Exception: pass
                return
            except Exception: pass
        toggler = getattr(self.app, "toggle_scan", None)
        if callable(toggler):
            try:
                if self._get_scan_state() != bool(state):
                    toggler(idx)
                    try: self.app._notify_server()
                    except Exception: pass
            except Exception: pass

    def _on_scan_toggle(self, _=None):
        try:
            idx = self._get_active_chan_safe()
            if idx == 3:
                self._update_scan_visual(True); return
            cur = self._get_scan_state()
            self._set_scan_state(not cur)
            self._update_scan_visual(not cur)
        except Exception:
            pass

    def _update_scan_visual(self, enabled=None):
        if enabled is None: enabled = self._get_scan_state()
        try: self.canvas.itemconfig(self.scan_text, text="SCAN" if enabled else "")
        except Exception: pass
        try:
            self.canvas.itemconfig(self.scan_btn_bg, fill=("#" if enabled else ""), outline=("" if enabled else ""))
            self.canvas.itemconfig(self.scan_btn_txt, fill=("#dff6df" if enabled else "#d3e9cf"))
        except Exception: pass


    def _draw_ptt_mode_switch(self):
        '''
        Invisible PTT mode hotspot:
        - Button bezel and text are created but set to state="hidden" (not visible).
        - An invisible hotspot is implemented via a canvas-wide click handler that checks the bbox.
        - Orange lamp lights when TOGGLE is active.
        '''
        c = self.canvas
        # User-specified placement and sizes
        x0, y0 = 26, 280
        x1, y1 = x0 + 64, y0 + 24

        # Save bbox for hotspot detection
        self.ptt_hitbox = (x0, y0, x1, y1)

        if not hasattr(self, "ptt_mode_bg"):
            # Hidden bezel & text (not drawn visually)
            self.ptt_mode_bg  = c.create_rectangle(x0, y0, x1, y1, fill="", outline="#8fa08a", width=2, state="hidden")
            self.ptt_mode_txt = c.create_text((x0 + x1)//2, (y0 + y1)//2,
                                              text="", fill="#d3e9cf", font=("Consolas", 9, "bold"))
            # Hide label text explicitly
            try:
                c.itemconfig(self.ptt_mode_txt, state="hidden")
            except Exception:
                pass

            # Keep binds for completeness (won't be visible, but harmless)
            c.tag_bind(self.ptt_mode_bg,  "<Button-1>", self._on_ptt_mode_toggle)
            c.tag_bind(self.ptt_mode_txt, "<Button-1>", self._on_ptt_mode_toggle)

            # Orange lamp to the right (visible)
            lamp_x0, lamp_y0 = x1 + 13, y0 + 7
            lamp_x1, lamp_y1 = lamp_x0 + 10, lamp_y0 + 10
            self.ptt_toggle_lamp = c.create_oval(lamp_x0, lamp_y0, lamp_x1, lamp_y1,
                                                 fill="#1a140a", outline="#332212")

            # Canvas-wide hotspot: clicking anywhere inside the bbox toggles PTT
            try:
                self.canvas.bind("<Button-1>", self._on_canvas_click_ptt, add="+")
            except Exception:
                pass

        self._update_ptt_mode_visual()

    def _on_canvas_click_ptt(self, event):
        """Global click router: toggles PTT if click is inside the PTT bbox."""
        try:
            x0, y0, x1, y1 = self.ptt_hitbox
        except Exception:
            return
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            self._on_ptt_mode_toggle()
            return "break"

    def _on_ptt_mode_toggle(self, _=None):
        try:
            cur = (self.app.ptt_mode.get() or "hold").strip().lower()
        except Exception:
            cur = "hold"
        new_mode = "toggle" if cur != "toggle" else "hold"
        try:
            self.app.ptt_mode.set(new_mode)
            try:
                self.app._save_user_config_all()
            except Exception:
                pass
            try:
                self.app.sounds.play_switch()
            except Exception:
                pass
        finally:
            self._update_ptt_mode_visual()

    def _update_ptt_mode_visual(self):
        try:
            mode = (self.app.ptt_mode.get() or "hold").strip().lower()
        except Exception:
            mode = "hold"
        # Don't show text (Option A) — keep hidden
        try:
            if hasattr(self, "ptt_mode_txt"):
                self.canvas.itemconfig(self.ptt_mode_txt, state="hidden")
        except Exception:
            pass
        # Lamp state
        on = (mode == 'toggle')
        fill = '#ff9a2e' if on else '#1a140a'
        outl = '#7a4a16' if on else '#332212'
        try:
            self.canvas.itemconfig(self.ptt_toggle_lamp, fill=fill, outline=outl)
        except Exception:
            pass
