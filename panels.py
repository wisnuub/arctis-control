"""
Settings + small windows for Arctis Control: which monitor the badge sits on,
which events show the Steam-style popup and on which screen, the headset's own
auto-off timer, and the battery health panel. Settings live in settings.json; the dict returned by
load_settings() is shared and updated in place so the poller sees changes.
"""
import ctypes
import json
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

import updater
from paths import DATA_DIR
from version import __version__
SETTINGS_FILE = DATA_DIR / "settings.json"

POPUP_EVENTS = [  # key, label, default
    ("low20", "Battery low (20%)", True),
    ("low10", "Battery low (10%)", True),
    ("low5", "Battery critical (5%)", True),
    ("full", "Fully charged", True),
    ("connected", "Headset turned on", True),
    ("disconnected", "Headset turned off", True),
    ("charging", "Charger plugged in", False),
    ("unplugged", "Charger unplugged", False),
    ("update", "Update available", True),
]
DEFAULTS = {
    "badge_monitor": None,        # device name like \\.\DISPLAY2; None = first non-main screen
    "popup_monitor": "active",    # "active" = screen of the active window, else a device name
    "popups": {k: on for k, _, on in POPUP_EVENTS},
    # Auto-off is decided by this app (no sound out AND no mic input for N minutes),
    # not by the headset's own timer, which is kept at "never" so it can't cut music.
    "auto_off": True,
    "auto_off_minutes": 30,
    "auto_off_applied": False,
    "check_updates": True,    # False = set the headset's own timer to never on next connect
}
AUTO_OFF_MAX = 240

BG, PANEL, BORDER, ACCENT, DIM = "#202020", "#2b2b2b", "#3a3a3a", "#4cc2ff", "#a0a0a0"


def load_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        saved = {}
    s = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    s["popups"] = {**DEFAULTS["popups"], **saved.get("popups", {})}
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def monitor_list():
    """[{name, rect, work, primary, label}] with labels like 'Monitor 2 (left)'."""
    found = []

    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        found.append({"name": mi.szDevice, "primary": bool(mi.dwFlags & 1),
                      "rect": (m.left, m.top, m.right, m.bottom),
                      "work": (w.left, w.top, w.right, w.bottom)})
        return 1

    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)(cb)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc, 0)
    main = next((m for m in found if m["primary"]), found[0] if found else None)
    for m in found:
        num = "".join(ch for ch in m["name"] if ch.isdigit()) or "?"
        if m["primary"]:
            side = "main"
        else:
            side = "left" if m["rect"][0] < main["rect"][0] else "right"
        m["label"] = f"Monitor {num} ({side})"
    return sorted(found, key=lambda m: m["name"])


def find_monitor(name):
    return next((m for m in monitor_list() if m["name"] == name), None)


def _dark_check(parent, text, var):
    return tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg="white",
                          selectcolor=PANEL, activebackground=BG, activeforeground="white",
                          highlightthickness=0, anchor="w", font=("Segoe UI", 9))


def _dark_radio(parent, text, var, value):
    return tk.Radiobutton(parent, text=text, variable=var, value=value, bg=BG, fg="white",
                          selectcolor=PANEL, activebackground=BG, activeforeground="white",
                          highlightthickness=0, anchor="w", font=("Segoe UI", 9))


def _button(parent, text, command, primary=False):
    b = tk.Label(parent, text=text, bg=ACCENT if primary else PANEL,
                 fg="black" if primary else "white", padx=14, pady=4, cursor="hand2",
                 font=("Segoe UI", 9, "bold" if primary else "normal"))
    b.bind("<Button-1>", lambda e: command())
    return b


class SettingsWindow:
    """Popup settings: which events show the popup, on which screen. Test / Save / Cancel."""

    def __init__(self, root, settings, test_popup, icon, on_save=None, on_quit=None):
        self.settings, self.test_popup, self.on_save = settings, test_popup, on_save
        self.on_quit, self.update_info, self._result = on_quit, None, None
        win = self.win = tk.Toplevel(root, bg=BG)
        win.title("Arctis Control settings")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        if icon:
            win.iconbitmap(icon)
        body = tk.Frame(win, bg=BG, padx=18, pady=14)
        body.pack()

        tk.Label(body, text="Show a popup when", bg=BG, fg="white",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.vars = {}
        for i, (key, label, _) in enumerate(POPUP_EVENTS):
            v = self.vars[key] = tk.BooleanVar(value=settings["popups"].get(key, False))
            _dark_check(body, label, v).grid(row=1 + i // 2, column=i % 2, sticky="w",
                                              padx=(0, 18), pady=1)

        r = 2 + len(POPUP_EVENTS) // 2
        tk.Label(body, text="Show it on", bg=BG, fg="white",
                 font=("Segoe UI", 10, "bold")).grid(row=r, column=0, columnspan=2,
                                                     sticky="w", pady=(12, 0))
        self.screen = tk.StringVar(value=settings.get("popup_monitor") or "active")
        _dark_radio(body, "The screen I'm using (active window)", self.screen, "active").grid(
            row=r + 1, column=0, columnspan=2, sticky="w")
        for j, m in enumerate(monitor_list()):
            _dark_radio(body, m["label"], self.screen, m["name"]).grid(
                row=r + 2 + j, column=0, columnspan=2, sticky="w")

        h = r + 3 + len(monitor_list())
        tk.Label(body, text="Headset", bg=BG, fg="white",
                 font=("Segoe UI", 10, "bold")).grid(row=h, column=0, columnspan=2,
                                                     sticky="w", pady=(12, 0))
        row = tk.Frame(body, bg=BG)
        row.grid(row=h + 1, column=0, columnspan=2, sticky="w")
        self.auto_off = tk.BooleanVar(value=settings.get("auto_off", True))
        _dark_check(row, "Turn off when no sound for", self.auto_off).pack(side="left")
        self.minutes = tk.Spinbox(row, from_=1, to=AUTO_OFF_MAX, width=4, justify="center",
                                  bg=PANEL, fg="white", buttonbackground=PANEL,
                                  insertbackground="white", relief="flat",
                                  font=("Segoe UI", 9))
        self.minutes.delete(0, "end")
        self.minutes.insert(0, str(settings.get("auto_off_minutes", 30)))
        self.minutes.pack(side="left", padx=(4, 4))
        tk.Label(row, text="minutes", bg=BG, fg="white", font=("Segoe UI", 9)).pack(side="left")
        tk.Label(body, text="Sound in the headset or on your mic counts as activity. "
                            "Works while Arctis Control runs.",
                 bg=BG, fg=DIM, font=("Segoe UI", 8)).grid(row=h + 2, column=0, columnspan=2,
                                                           sticky="w")

        a = h + 3
        tk.Label(body, text="About", bg=BG, fg="white",
                 font=("Segoe UI", 10, "bold")).grid(row=a, column=0, columnspan=2,
                                                     sticky="w", pady=(12, 0))
        about = tk.Frame(body, bg=BG)
        about.grid(row=a + 1, column=0, columnspan=2, sticky="we")
        tk.Label(about, text=f"Arctis Control v{__version__}", bg=BG, fg="white",
                 font=("Segoe UI", 9)).pack(side="left")
        self.update_btn = _button(about, "Update now", self.update_now, primary=True)
        _button(about, "Check for updates", self.check_updates).pack(side="right")
        self.status = tk.Label(body, text="", bg=BG, fg=DIM, anchor="w", font=("Segoe UI", 8))
        self.status.grid(row=a + 2, column=0, columnspan=2, sticky="w")
        self.auto_update = tk.BooleanVar(value=settings.get("check_updates", True))
        _dark_check(body, "Check for updates automatically (once a day)", self.auto_update).grid(
            row=a + 3, column=0, columnspan=2, sticky="w")

        buttons = tk.Frame(body, bg=BG)
        buttons.grid(row=a + 4, column=0, columnspan=2, sticky="e", pady=(16, 0))
        _button(buttons, "Test", self.test).pack(side="left", padx=(0, 8))
        _button(buttons, "Cancel", win.destroy).pack(side="left", padx=(0, 8))
        _button(buttons, "Save", self.save, primary=True).pack(side="left")
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        mon = find_monitor(settings.get("badge_monitor") or "") or next(
            (m for m in monitor_list() if not m["primary"]), monitor_list()[0])
        l, t, rr, b = mon["work"]
        win.geometry(f"+{(l + rr - win.winfo_width()) // 2}+{(t + b - win.winfo_height()) // 2}")
        win.focus_force()

    # --- updates (network work off the Tk thread, polled back in) ---------------
    def check_updates(self):
        self.status.config(text="Checking GitHub…")
        self.update_btn.pack_forget()
        self._result = None

        def work():
            try:
                self._result = updater.check()
            except Exception as e:
                self._result = {"error": str(e)}
        threading.Thread(target=work, daemon=True).start()
        self.win.after(200, self._poll_check)

    def _poll_check(self):
        if not self.win.winfo_exists():
            return
        if self._result is None:
            self.win.after(200, self._poll_check)
            return
        r = self._result
        if "error" in r:
            self.status.config(text="Couldn't reach GitHub. Try again later.")
        elif r["newer"]:
            self.update_info = r
            self.status.config(text=f"{r['tag']} is available.")
            self.update_btn.pack(side="right", padx=(8, 0))
        else:
            self.status.config(text="You're on the latest version.")

    def update_now(self):
        if not self.update_info:
            return
        self.status.config(text=f"Downloading {self.update_info['tag']}…")
        self._result = None

        def work():
            try:
                self._result = {"quit": updater.install(self.update_info)}
            except Exception as e:
                self._result = {"error": str(e)}
        threading.Thread(target=work, daemon=True).start()
        self.win.after(200, self._poll_install)

    def _poll_install(self):
        if not self.win.winfo_exists():
            return
        if self._result is None:
            self.win.after(200, self._poll_install)
        elif "error" in self._result:
            self.status.config(text="Update failed. Download it from the Releases page instead.")
        elif self._result["quit"] and self.on_quit:
            self.status.config(text="Restarting with the new version…")
            self.win.after(500, self.on_quit)       # swap script waits for us to exit
        else:
            self.status.config(text="Opened the release page in your browser.")

    def test(self):
        screen = self.screen.get()
        self.test_popup(None if screen == "active" else (find_monitor(screen) or {}).get("rect"))

    def save(self):
        self.settings["popups"] = {k: v.get() for k, v in self.vars.items()}
        self.settings["popup_monitor"] = self.screen.get()
        self.settings["check_updates"] = self.auto_update.get()
        try:
            minutes = max(1, min(AUTO_OFF_MAX, int(self.minutes.get())))
        except ValueError:
            minutes = self.settings.get("auto_off_minutes", 30)
        self.settings["auto_off"], self.settings["auto_off_minutes"] = self.auto_off.get(), minutes
        save_settings(self.settings)
        self.win.destroy()
        if self.on_save:
            self.on_save()


class HealthPanel:
    """Battery health stats in a small panel above the badge (toasts are silent with
    Do Not Disturb on). Closes on Esc or clicking elsewhere."""

    def __init__(self, root, anchor_right, anchor_top, rows, title="Battery health"):
        win = self.win = tk.Toplevel(root, bg=BORDER)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        body = tk.Frame(win, bg=BG, padx=16, pady=12)
        body.pack(padx=1, pady=1)
        tk.Label(body, text=title, bg=BG, fg="white", anchor="w",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                     sticky="w", pady=(0, 6))
        for i, (k, v) in enumerate(rows, start=1):
            tk.Label(body, text=k, bg=BG, fg=DIM, anchor="w",
                     font=("Segoe UI", 9)).grid(row=i, column=0, sticky="w", padx=(0, 16))
            tk.Label(body, text=v, bg=BG, fg="white", anchor="w",
                     font=("Segoe UI", 9)).grid(row=i, column=1, sticky="w")
        win.update_idletasks()
        win.geometry(f"+{anchor_right - win.winfo_width()}+{anchor_top - win.winfo_height() - 8}")
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<Button-1>", lambda e: win.destroy())
        win.bind("<FocusOut>", lambda e: win.after(150, self._maybe_close))
        win.focus_force()

    def _maybe_close(self):
        if self.win.winfo_exists() and self.win.focus_get() is None:
            self.win.destroy()
