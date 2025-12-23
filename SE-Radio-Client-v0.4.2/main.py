import tkinter as tk
import sys
from app.app import App
from pathlib import Path

def _set_icon(root: tk.Tk):
    """Set the window/taskbar icon using app/assets/icon.ico or icon.png."""
    try:
        base = Path(__file__).resolve().parent / "app" / "assets"
        ico = base / "icon.ico"
        png = base / "icon.png"

        # Prefer ICO for Windows taskbar
        if sys.platform.startswith("win"):
            if ico.exists():
                try:
                    root.iconbitmap(default=str(ico))
                    print("[Icon] Loaded .ico for Windows taskbar.")
                    return
                except Exception as e:
                    print("[Icon] ICO load failed:", e)

        # Fallback: PNG for other OS (title bar)
        if png.exists():
            try:
                _img = tk.PhotoImage(file=str(png))
                root._app_icon_img = _img  # prevent garbage collection
                root.iconphoto(True, _img)
                print("[Icon] Loaded .png as window icon.")
            except Exception as e:
                print("[Icon] PNG load failed:", e)
    except Exception as e:
        print("[Icon] Unexpected error:", e)

def main():
    root = tk.Tk()
    _set_icon(root)  # Set taskbar/titlebar icon
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
