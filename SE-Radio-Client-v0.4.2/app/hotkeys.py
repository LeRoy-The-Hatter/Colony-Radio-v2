"""
Lightweight global input helper (keyboard + optional gamepad buttons).

- Keyboard events are captured via the `keyboard` package when available.
- Gamepad/joystick buttons are polled via pygame (optional, opt-in).
- Tracks currently pressed tokens so the app can read combos.
- Provides a simple capture flow for keybind setup.
"""

from __future__ import annotations

import threading
import time
import ctypes
from typing import Callable, Optional, Set, Tuple

try:
    import keyboard as _kb  # type: ignore

    _HAVE_KEYBOARD = True
except Exception:
    _kb = None
    _HAVE_KEYBOARD = False

try:
    from pynput import keyboard as _pynput  # type: ignore

    _HAVE_PYNPUT = True
except Exception:
    _pynput = None
    _HAVE_PYNPUT = False

try:
    import pygame  # type: ignore

    _HAVE_PYGAME = True
except Exception:
    pygame = None
    _HAVE_PYGAME = False

_KEYBOARD_ACTIVE = False
_GAMEPAD_ACTIVE = False


class _XInputPoller:
    """Minimal XInput poller (Windows) to get reliable button up/down."""

    def __init__(self):
        self.ok = False
        self._xinput = None
        self._load()

    def _load(self):
        if not hasattr(ctypes, "windll"):
            return
        # Try common DLLs
        for dll in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                lib = ctypes.windll.LoadLibrary(dll)
                self._xinput = lib
                break
            except Exception:
                self._xinput = None
        if not self._xinput:
            return
        try:
            class XINPUT_GAMEPAD(ctypes.Structure):
                _fields_ = [
                    ("wButtons", ctypes.c_ushort),
                    ("bLeftTrigger", ctypes.c_ubyte),
                    ("bRightTrigger", ctypes.c_ubyte),
                    ("sThumbLX", ctypes.c_short),
                    ("sThumbLY", ctypes.c_short),
                    ("sThumbRX", ctypes.c_short),
                    ("sThumbRY", ctypes.c_short),
                ]

            class XINPUT_STATE(ctypes.Structure):
                _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", XINPUT_GAMEPAD)]

            self._XINPUT_STATE = XINPUT_STATE
            self._STATE_ARR = XINPUT_STATE * 4
            self.ok = True
        except Exception:
            self._xinput = None
            self.ok = False

    def poll(self) -> tuple[Set[str], dict]:
        tokens: Set[str] = set()
        details = {}
        if not self.ok or not self._xinput:
            return tokens, details
        for i in range(4):
            st = self._STATE_ARR()
            try:
                res = self._xinput.XInputGetState(ctypes.c_uint(i), ctypes.byref(st[i]))
            except Exception:
                continue
            if res != 0:
                continue
            g = st[i].Gamepad
            prefix = f"X{i+1}"
            name = f"XInput Pad #{i+1}"
            buttons = {
                0x1000: "BtnA",
                0x2000: "BtnB",
                0x4000: "BtnX",
                0x8000: "BtnY",
                0x0100: "BtnLB",
                0x0200: "BtnRB",
                0x0010: "BtnStart",
                0x0020: "BtnBack",
                0x0040: "BtnLS",
                0x0080: "BtnRS",
                0x0001: "PadUp",
                0x0002: "PadDown",
                0x0004: "PadLeft",
                0x0008: "PadRight",
            }
            for mask, tok in buttons.items():
                if g.wButtons & mask:
                    full = f"{prefix}{tok}"
                    tokens.add(full)
                    details[full] = name
            # Triggers as buttons (light threshold)
            if g.bLeftTrigger > 30:
                full = f"{prefix}TrigL"
                tokens.add(full)
                details[full] = name
            if g.bRightTrigger > 30:
                full = f"{prefix}TrigR"
                tokens.add(full)
                details[full] = name
        return tokens, details


def have_pynput() -> bool:
    """Legacy helper kept for the app; indicates if the keyboard hook is usable."""
    return bool(_KEYBOARD_ACTIVE)


def have_mouse() -> bool:
    return False


def have_gamepad() -> bool:
    return bool(_GAMEPAD_ACTIVE)


class GlobalKeyListener:
    """Global listener with keyboard hook + optional pygame gamepad polling."""

    def __init__(self, app_ref):
        self.app = app_ref
        self.listener = None
        self.active = False
        self._keyboard_active = False
        self._keyboard_backend = None  # "keyboard" | "pynput" | None
        self._xinput = _XInputPoller()

        self._lock = threading.RLock()
        self._pressed: Set[str] = set()
        self._pressed_gamepad: Set[str] = set()
        self._gamepad_details = {}
        self._gamepad_thread = None
        self._gamepad_stop = threading.Event()
        self._gamepad_enabled = True
        self._gamepad_poll_interval = 0.02  # 50 Hz
        self._gamepad_hat_state = {}
        self._joysticks = {}

        # Capture state
        self._capturing = False
        self._capture_target: Optional[str] = None
        self._capture_on_done: Optional[Callable[[str, Tuple[str, ...]], None]] = None
        self._capture_on_cancel: Optional[Callable[[str], None]] = None
        self._capture_release_window_ms: int = 250
        self._capture_timeout_ms: int = 10000
        self._capture_timer: Optional[threading.Timer] = None
        self._capture_timeout_timer: Optional[threading.Timer] = None
        self._capture_last_activity_ms: int = 0
        self._pressed_capture: Set[str] = set()
        self._capture_last_non_empty: Tuple[str, ...] = tuple()

        try:
            pref = getattr(app_ref, "joystick_enabled", None)
            if hasattr(pref, "get"):
                self._gamepad_enabled = bool(pref.get())
            elif isinstance(pref, bool):
                self._gamepad_enabled = pref
        except Exception:
            pass

    # ---------------- Lifecycle ----------------

    def start(self, enable_gamepad: Optional[bool] = None):
        if enable_gamepad is not None:
            self._gamepad_enabled = bool(enable_gamepad)
        if self.active:
            return
        self.active = True
        self._start_keyboard_hook()
        self._start_gamepad_polling()
        if not self._keyboard_active and not (self._gamepad_thread and self._gamepad_thread.is_alive()):
            self.active = False

    def stop(self):
        with self._lock:
            self._stop_keyboard_locked()
            self._stop_gamepad_locked()
            self.active = False
            globals()["_KEYBOARD_ACTIVE"] = False
            globals()["_GAMEPAD_ACTIVE"] = False
            self._pressed.clear()
            self._pressed_gamepad.clear()
            try:
                if hasattr(self.app, "_pressed_global"):
                    self.app._pressed_global.clear()
            except Exception:
                pass
            self._cancel_capture_locked()

    def snapshot_pressed_normal(self) -> Set[str]:
        """Thread-safe snapshot of pressed inputs (keyboard + gamepad)."""
        with self._lock:
            return set(self._pressed)

    # ---------------- Capture API ----------------

    def begin_capture(
        self,
        target_name: str,
        on_done: Callable[[str, Tuple[str, ...]], None],
        on_cancel: Optional[Callable[[str], None]] = None,
        release_window_ms: int = 250,
        timeout_ms: int = 10000,
    ) -> bool:
        if not (_HAVE_KEYBOARD or _HAVE_PYNPUT or (_HAVE_PYGAME and self._gamepad_enabled)):
            return False
        with self._lock:
            if self._capturing:
                return False
            self._capturing = True
            self._capture_target = target_name
            self._capture_on_done = on_done
            self._capture_on_cancel = on_cancel
            self._capture_release_window_ms = max(50, int(release_window_ms))
            self._capture_timeout_ms = max(1000, int(timeout_ms))
            self._capture_last_activity_ms = self._now_ms()
            self._pressed_capture.clear()
            self._capture_last_non_empty = tuple()
            self._cancel_finalize_timer_locked()

            # Safety timeout so the UI never gets stuck
            self._capture_timeout_timer = threading.Timer(
                self._capture_timeout_ms / 1000.0, self._finalize_capture_timeout
            )
            self._capture_timeout_timer.daemon = True
            self._capture_timeout_timer.start()

            if hasattr(self.app, "_waiting_bind"):
                try:
                    self.app._waiting_bind = True
                except Exception:
                    pass
            return True

    def cancel_capture(self):
        with self._lock:
            if not self._capturing:
                return
            target = self._capture_target
            cb = self._capture_on_cancel
            self._cancel_capture_locked()
        if cb and target is not None:
            try:
                cb(target)
            except Exception:
                pass

    # ---------------- Internals ----------------

    def _start_keyboard_hook(self):
        # Try the keyboard package first; fall back to pynput if it fails
        if self._start_keyboard_via_keyboard():
            self._keyboard_backend = "keyboard"
            return
        if self._start_keyboard_via_pynput():
            self._keyboard_backend = "pynput"
            return
        self._keyboard_backend = None
        self._keyboard_active = False
        globals()["_KEYBOARD_ACTIVE"] = False

    def _start_keyboard_via_keyboard(self) -> bool:
        if not _HAVE_KEYBOARD:
            return False
        try:
            self.listener = _kb.hook(self._handle_event, suppress=False)
            self._keyboard_active = True
            globals()["_KEYBOARD_ACTIVE"] = True
            return True
        except Exception as exc:
            try:
                print(f"[HOTKEYS] keyboard hook failed: {exc}")
            except Exception:
                pass
            self.listener = None
            self._keyboard_active = False
            globals()["_KEYBOARD_ACTIVE"] = False
            return False

    def _start_keyboard_via_pynput(self) -> bool:
        if not _HAVE_PYNPUT:
            return False
        try:
            def _on_press(key):
                name = self._pynput_key_name(key)
                if not name:
                    return
                evt = type("PynputEvent", (), {"event_type": "down", "name": name})
                self._handle_event(evt)

            def _on_release(key):
                name = self._pynput_key_name(key)
                if not name:
                    return
                evt = type("PynputEvent", (), {"event_type": "up", "name": name})
                self._handle_event(evt)

            listener = _pynput.Listener(on_press=_on_press, on_release=_on_release)
            listener.start()  # non-blocking; runs on its own thread
            self.listener = listener
            self._keyboard_active = True
            globals()["_KEYBOARD_ACTIVE"] = True
            try:
                print("[HOTKEYS] keyboard hook unavailable; using pynput fallback for global hotkeys")
            except Exception:
                pass
            return True
        except Exception as exc:
            try:
                print(f"[HOTKEYS] pynput hook failed: {exc}")
            except Exception:
                pass
            self.listener = None
            self._keyboard_active = False
            globals()["_KEYBOARD_ACTIVE"] = False
            return False

    def _pynput_key_name(self, key) -> str:
        """Normalize pynput Key/KeyCode to the same tokens _tokenize expects."""
        try:
            if hasattr(key, "char") and key.char:
                return str(key.char)
        except Exception:
            pass
        name = ""
        try:
            name = getattr(key, "name", "") or ""
        except Exception:
            name = ""
        if not name:
            try:
                name = str(key) or ""
            except Exception:
                name = ""
        if name.startswith("Key."):
            name = name[4:]
        name = name.replace("_l", "").replace("_r", "")
        if name.lower() == "altgr":
            name = "alt"
        return name

    def _stop_keyboard_locked(self):
        try:
            if self.listener:
                if self._keyboard_backend == "keyboard" and _HAVE_KEYBOARD:
                    _kb.unhook(self.listener)
                elif self._keyboard_backend == "pynput":
                    try:
                        self.listener.stop()
                    except Exception:
                        pass
        except Exception:
            pass
        self.listener = None
        self._keyboard_active = False
        self._keyboard_backend = None

    def _start_gamepad_polling(self):
        if not self._gamepad_enabled or not _HAVE_PYGAME:
            globals()["_GAMEPAD_ACTIVE"] = False
            return
        if self._gamepad_thread and self._gamepad_thread.is_alive():
            return
        self._gamepad_stop.clear()
        self._gamepad_thread = threading.Thread(target=self._gamepad_loop, daemon=True)
        self._gamepad_thread.start()

    def _stop_gamepad_locked(self):
        try:
            self._gamepad_stop.set()
        except Exception:
            pass
        th = self._gamepad_thread
        still_running = False
        if th and th.is_alive() and th is not threading.current_thread():
            try:
                # Give the poller thread a moment to exit cleanly before tearing down pygame
                th.join(timeout=1.5)
            except Exception:
                pass
            still_running = th.is_alive()
        elif th is threading.current_thread():
            still_running = True
        # Preserve the reference if the thread hasn't exited yet so we don't spawn a duplicate
        self._gamepad_thread = th if still_running else None
        to_release = set()
        with self._lock:
            to_release = set(self._pressed_gamepad)
            self._pressed_gamepad.clear()
            self._gamepad_details = {}
        for tok in to_release:
            self._handle_token(tok, False, origin="Gamepad", detail="poller stop")
        globals()["_GAMEPAD_ACTIVE"] = False

    def _gamepad_loop(self):
        if not _HAVE_PYGAME:
            return
        try:
            pygame.joystick.init()
        except Exception as exc:
            try:
                print(f"[HOTKEYS] pygame joystick init failed: {exc}")
            except Exception:
                pass
            return
        globals()["_GAMEPAD_ACTIVE"] = False
        while not self._gamepad_stop.is_set():
            try:
                pygame.event.pump()
            except Exception:
                pass
            events = []
            try:
                events = pygame.event.get()
            except Exception:
                events = []
            tokens_now: Set[str] = set()
            detail_map = {}
            force_up: Set[str] = set()
            try:
                count = pygame.joystick.get_count()
            except Exception:
                count = 0
            globals()["_GAMEPAD_ACTIVE"] = bool(count > 0)
            active_sleep = self._gamepad_poll_interval if count > 0 else max(0.05, self._gamepad_poll_interval)
            single_gamepad = count == 1

            # Handle explicit button/hats events first (captures UP even if polling is noisy)
            for ev in events:
                if self._gamepad_stop.is_set():
                    break
                etype = getattr(ev, "type", None)
                joy_idx = getattr(ev, "joy", None)
                btn = getattr(ev, "button", None)
                val = getattr(ev, "value", None)
                hat = getattr(ev, "hat", None)
                name = ""
                joy_prefix = f"Joy{(joy_idx or 0)+1}" if joy_idx is not None else "Joy"
                if joy_idx is not None and joy_idx < count:
                    try:
                        j = pygame.joystick.Joystick(joy_idx)
                        if not j.get_init():
                            j.init()
                        name = j.get_name()
                    except Exception:
                        name = f"Gamepad #{(joy_idx or 0)+1}"
                if etype == getattr(pygame, "JOYBUTTONDOWN", None) and btn is not None:
                    tok = f"{joy_prefix}Btn{btn+1}"
                    self._add_gamepad_token(tokens_now, detail_map, tok, name, single_gamepad)
                elif etype == getattr(pygame, "JOYBUTTONUP", None) and btn is not None:
                    tok = f"{joy_prefix}Btn{btn+1}"
                    force_up.add(tok)
                    if single_gamepad and not self._capturing:
                        force_up.add(self._generic_gamepad_token(tok))
                elif etype == getattr(pygame, "JOYHATMOTION", None) and hat is not None:
                    hx, hy = (val or (0, 0)) if isinstance(val, (tuple, list)) else (0, 0)
                    prefix = f"{joy_prefix}Hat{hat+1}"
                    hat_tokens = self._hat_tokens(prefix, hx, hy)
                    # Release previous hat tokens for this hat explicitly
                    prev_hat = set(self._gamepad_hat_state.get(prefix, []))
                    if single_gamepad and not self._capturing:
                        prev_hat |= {self._generic_gamepad_token(t) for t in prev_hat}
                    force_up |= prev_hat
                    for t in hat_tokens:
                        self._add_gamepad_token(tokens_now, detail_map, t, name, single_gamepad)
                    self._gamepad_hat_state[prefix] = set(hat_tokens)
            # Poll current state as a fallback (skipping tokens explicitly released via events)
            for idx in range(count):
                if self._gamepad_stop.is_set():
                    break
                try:
                    joy = self._joysticks.get(idx)
                    if joy is None:
                        joy = pygame.joystick.Joystick(idx)
                        joy.init()
                        self._joysticks[idx] = joy
                    try:
                        name = joy.get_name()
                    except Exception:
                        name = f"Gamepad #{idx+1}"
                    joy_prefix = f"Joy{idx+1}"
                    # Buttons
                    try:
                        btn_count = joy.get_numbuttons()
                    except Exception:
                        btn_count = 0
                    for b in range(btn_count):
                        try:
                            tok = f"{joy_prefix}Btn{b+1}"
                            if tok in force_up:
                                continue
                            if joy.get_button(b):
                                self._add_gamepad_token(tokens_now, detail_map, tok, name or f"Gamepad #{idx+1}", single_gamepad)
                        except Exception:
                            pass
                    # Hats (D-pad)
                    try:
                        hat_count = joy.get_numhats()
                    except Exception:
                        hat_count = 0
                    for h in range(hat_count):
                        try:
                            hx, hy = joy.get_hat(h)
                        except Exception:
                            hx, hy = 0, 0
                        prefix = f"{joy_prefix}Hat{h+1}"
                        hat_tokens = self._hat_tokens(prefix, hx, hy)
                        for t in hat_tokens:
                            self._add_gamepad_token(tokens_now, detail_map, t, name, single_gamepad)
                        self._gamepad_hat_state[prefix] = set(hat_tokens)
                except Exception:
                    continue
            # XInput fallback (reliable up/down on Windows pads)
            try:
                if self._xinput and self._xinput.ok:
                    x_tokens, x_details = self._xinput.poll()
                    tokens_now |= x_tokens
                detail_map.update(x_details)
            except Exception:
                pass

            self._sync_gamepad_tokens(tokens_now, detail_map)
            self._maybe_finalize_capture_idle()
            try:
                time.sleep(active_sleep)
            except Exception:
                pass
        globals()["_GAMEPAD_ACTIVE"] = False
        try:
            # Clean up joystick subsystem after the thread has fully exited to avoid races
            if _HAVE_PYGAME:
                pygame.joystick.quit()
        except Exception:
            pass

    def _sync_gamepad_tokens(self, tokens_now: Set[str], detail_map):
        with self._lock:
            prev = set(self._pressed_gamepad)
            prev_detail = dict(self._gamepad_details)
            # Trim cached joysticks if count shrank
            try:
                count = pygame.joystick.get_count() if _HAVE_PYGAME else 0
                stale = [k for k in self._joysticks.keys() if k >= count]
                for k in stale:
                    self._joysticks.pop(k, None)
            except Exception:
                pass
        newly_pressed = tokens_now - prev
        newly_released = prev - tokens_now
        for tok in newly_pressed:
            self._handle_token(tok, True, origin="Gamepad", detail=detail_map.get(tok, "Gamepad"))
        for tok in newly_released:
            self._handle_token(tok, False, origin="Gamepad", detail=prev_detail.get(tok, "Gamepad"))
        with self._lock:
            self._pressed_gamepad = set(tokens_now)
            self._gamepad_details = {tok: detail_map.get(tok, prev_detail.get(tok, "Gamepad")) for tok in tokens_now}

    def _hat_tokens(self, prefix: str, hx: int, hy: int) -> Set[str]:
        tokens = set()
        if hy > 0:
            tokens.add(f"{prefix}Up")
        elif hy < 0:
            tokens.add(f"{prefix}Down")
        if hx < 0:
            tokens.add(f"{prefix}Left")
        elif hx > 0:
            tokens.add(f"{prefix}Right")
        return tokens

    def _generic_gamepad_token(self, tok: str) -> str:
        """Strip device index so legacy configs like JoyBtn4 still work with a single device."""
        if tok.startswith("Joy") and len(tok) > 3 and tok[3].isdigit():
            i = 3
            while i < len(tok) and tok[i].isdigit():
                i += 1
            return "Joy" + tok[i:]
        return tok

    def _add_gamepad_token(self, tokens_now: Set[str], detail_map: dict, tok: str, name: str, add_generic: bool):
        tokens_now.add(tok)
        if name:
            detail_map[tok] = name
        # Add legacy alias only when not capturing a bind (prevents combos from recording both tokens)
        if add_generic and not self._capturing:
            generic = self._generic_gamepad_token(tok)
            tokens_now.add(generic)
            if name:
                detail_map[generic] = name

    def _maybe_finalize_capture_idle(self):
        """If a device only fires 'down' and never 'up', close capture after a short idle."""
        target = None
        cb = None
        combo = None
        with self._lock:
            if not self._capturing:
                return
            now = self._now_ms()
            if (
                self._capture_last_non_empty
                and now - self._capture_last_activity_ms >= self._capture_release_window_ms
            ):
                combo = self._capture_last_non_empty
                target = self._capture_target
                cb = self._capture_on_done
                self._cancel_capture_locked()
        if cb and target is not None and combo is not None:
            try:
                cb(target, combo)
            except Exception:
                pass

    def _handle_event(self, event):
        if getattr(event, "event_type", "") not in ("down", "up"):
            return
        tok = self._tokenize(event)
        if not tok:
            return
        detail = ""
        try:
            detail = getattr(event, "name", "") or ""
        except Exception:
            detail = ""
        self._handle_token(tok, is_press=(event.event_type == "down"), origin="Keyboard", detail=detail)

    def _handle_token(self, token: str, is_press: bool, origin: str = "Keyboard", detail: str = ""):
        if not token or self._should_ignore(token):
            return
        capturing = False

        with self._lock:
            capturing = self._capturing
            if capturing:
                if is_press:
                    self._pressed_capture.add(token)
                    if self._pressed_capture:
                        self._capture_last_non_empty = tuple(sorted(self._pressed_capture))
                else:
                    self._pressed_capture.discard(token)
                self._capture_last_activity_ms = self._now_ms()
                self._cancel_finalize_timer_locked()
                self._schedule_capture_finalize_locked()
            else:
                if is_press:
                    self._pressed.add(token)
                    try:
                        if hasattr(self.app, "_pressed_global"):
                            self.app._pressed_global.add(token)
                    except Exception:
                        pass
                else:
                    self._pressed.discard(token)
                    try:
                        if hasattr(self.app, "_pressed_global"):
                            self.app._pressed_global.discard(token)
                    except Exception:
                        pass

        # Mirror to debug viewer if present
        self._emit_debug(origin, token, "down" if is_press else "up", capturing=capturing, detail=detail)

        # Drive the app's input update outside the lock
        if not capturing and not getattr(self.app, "_waiting_bind", False):
            try:
                if hasattr(self.app, "_request_input_refresh"):
                    self.app._request_input_refresh(source=f"global-{origin.lower()}-{ 'press' if is_press else 'release' }")
                else:
                    self.app._update_ptt_and_channels(source=f"global-{origin.lower()}-{ 'press' if is_press else 'release' }")
            except Exception:
                pass

    def _emit_debug(self, origin: str, token: str, action: str, capturing: bool = False, detail: str = ""):
        try:
            if not getattr(self.app, "_input_debug_enabled", False):
                return
            snapshot = ()
            try:
                with self._lock:
                    snapshot = tuple(
                        sorted(self._pressed_capture if capturing else self._pressed)
                    )
            except Exception:
                snapshot = ()
            if hasattr(self.app, "_enqueue_input_debug_event"):
                self.app._enqueue_input_debug_event(
                    origin, token, action, detail=detail or "global", pressed_snapshot=snapshot
                )
        except Exception:
            pass

    def _should_ignore(self, token: str) -> bool:
        try:
            if hasattr(self.app, "_is_token_ignored"):
                return bool(self.app._is_token_ignored(token))
        except Exception:
            pass
        return False

    def _tokenize(self, event) -> Optional[str]:
        # Synthetic tokens can pass radio_token to skip parsing
        if hasattr(event, "radio_token"):
            try:
                tok = getattr(event, "radio_token")
                return str(tok) if tok else None
            except Exception:
                return None
        try:
            name = getattr(event, "name", "") or ""
        except Exception:
            name = ""
        n = str(name).replace("_", " ").strip().lower()
        if not n:
            return None
        special = {
            "ctrl": "Ctrl",
            "control": "Ctrl",
            "shift": "Shift",
            "alt": "Alt",
            "option": "Alt",
            "meta": "Alt",
            "windows": "Win",
            "win": "Win",
            "command": "Win",
            "cmd": "Win",
            "space": "Space",
            "esc": "Escape",
            "escape": "Escape",
            "tab": "Tab",
            "enter": "Enter",
            "return": "Enter",
            "backspace": "Backspace",
            "delete": "Delete",
            "del": "Delete",
            "home": "Home",
            "end": "End",
            "pageup": "PageUp",
            "page up": "PageUp",
            "pagedown": "PageDown",
            "page down": "PageDown",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "capslock": "CapsLock",
            "caps lock": "CapsLock",
            "numlock": "NumLock",
            "num lock": "NumLock",
            "scrolllock": "ScrollLock",
            "scroll lock": "ScrollLock",
        }
        if n in special:
            return special[n]
        if n.startswith("f") and n[1:].isdigit():
            try:
                return f"F{int(n[1:])}"
            except Exception:
                return None
        if len(n) == 1:
            return n.upper()
        return name

    def _schedule_capture_finalize_locked(self):
        self._cancel_finalize_timer_locked()
        self._capture_timer = threading.Timer(
            self._capture_release_window_ms / 1000.0, self._finalize_capture_if_idle
        )
        self._capture_timer.daemon = True
        self._capture_timer.start()

    def _finalize_capture_if_idle(self):
        with self._lock:
            if not self._capturing:
                return
            now = self._now_ms()
            since_last = now - self._capture_last_activity_ms
            if since_last < self._capture_release_window_ms:
                # Activity too recent; reschedule for remaining time
                remaining = max(50, self._capture_release_window_ms - since_last)
                self._capture_timer = threading.Timer(
                    remaining / 1000.0, self._finalize_capture_if_idle
                )
                self._capture_timer.daemon = True
                self._capture_timer.start()
                return
        # Safe to finalize outside the lock
        self._finalize_capture()

    def _finalize_capture(self):
        with self._lock:
            if not self._capturing:
                return
            if self._pressed_capture:
                combo = tuple(sorted(self._pressed_capture))
            elif self._capture_last_non_empty:
                combo = self._capture_last_non_empty
            else:
                combo = tuple()
            target = self._capture_target
            cb = self._capture_on_done
            self._cancel_capture_locked()
        if cb and target is not None:
            try:
                cb(target, combo)
            except Exception:
                pass

    def _finalize_capture_timeout(self):
        with self._lock:
            if not self._capturing:
                return
            target = self._capture_target
            cb = self._capture_on_cancel
            self._cancel_capture_locked()
        if cb and target is not None:
            try:
                cb(target)
            except Exception:
                pass

    def _cancel_capture_locked(self):
        self._cancel_finalize_timer_locked()
        if self._capture_timeout_timer:
            try:
                self._capture_timeout_timer.cancel()
            except Exception:
                pass
        self._capture_timeout_timer = None

        self._capturing = False
        self._capture_target = None
        self._capture_on_done = None
        self._capture_on_cancel = None
        self._pressed_capture.clear()
        self._capture_last_non_empty = tuple()
        if hasattr(self.app, "_waiting_bind"):
            try:
                self.app._waiting_bind = False
            except Exception:
                pass

    def _cancel_finalize_timer_locked(self):
        if self._capture_timer:
            try:
                self._capture_timer.cancel()
            except Exception:
                pass
        self._capture_timer = None

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    # Compatibility shims for caller expectations
    def zero_gamepad_axes(self):
        return

    def set_gamepad_polling(self, enabled: bool):
        self._gamepad_enabled = bool(enabled)
        if enabled:
            self._start_gamepad_polling()
        else:
            self._stop_gamepad_locked()
