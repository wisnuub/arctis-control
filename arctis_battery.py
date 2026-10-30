"""
Tray battery monitor for the SteelSeries Arctis 7 (2019, USB PID 0x12AD).

Shows the headset battery % as a small badge in the bottom-right corner of the
second screen (the tray only exists on the main screen's taskbar) and pops a
Windows toast plus a Steam-style overlay (bottom-right of the screen you're
playing on, click-through, never takes focus) when it
drops to 20 / 10 / 5 %. The headset can't report battery health, so it is
estimated from logged readings: drain rate while in use -> projected runtime
per full charge vs the rated 24 h, plus sudden voltage-sag drops. Talks to the dongle over HID directly (same protocol
HeadsetControl uses), so SteelSeries GG can keep running alongside it.
"""
import ctypes
import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont

import hid
from comtypes import CLSCTX_ALL
from pycaw.pycaw import IAudioMeterInformation
import updater
from panels import (HealthPanel, SettingsWindow, find_monitor, load_settings, monitor_list,
                    save_settings)
from dts import DTSController
from mixer import SWEEP_MS, Mixer, MixerPanel
from winotify import Notification, audio

VID, PID = 0x1038, 0x12AD
CABLE_PID = 0x12AE                # headset on its charging cable shows up as "Arctis 7 Bootloader"
CABLE_POLL = 2                    # seconds between cable checks
USAGE_PAGE, USAGE = 0xFF43, 0x202
POLL_SECONDS = 60
THRESHOLDS = (20, 10, 5)          # toast once per discharge at each of these
REARM_MARGIN = 5                  # re-arm a threshold once battery is this far above it

RATED_HOURS = 24                  # SteelSeries spec for the Arctis 7 (2019)
HISTORY_DAYS = 14                 # readings older than this are ignored for health
GAP_SECONDS = 5 * 60              # longer gap = headset was off / app not running
CHARGE_RISE = 3                   # level this far above a stretch's low = it was charging
SAG_DROP = 8                      # one-poll drop this big = voltage sag, not normal drain
MIN_HOURS, MIN_DROP = 2, 5        # data needed before trusting the estimate
UNPLUG_SETTLE = 3 * 60            # reading sags for a few minutes after unplugging

from paths import DATA_DIR, RES_DIR
APP_ID = "Wisnu.ArctisControl"    # Windows app identity: toast name/icon, taskbar grouping
APP_NAME = "Arctis Control"
SHOW_EVENT = "ArctisControlShowMixer"   # set by a second launch (Start menu / taskbar)
SETTINGS = load_settings()        # shared with panels.SettingsWindow, edited in place
HISTORY_FILE = DATA_DIR / "history.csv"
STATE_FILE = DATA_DIR / "state.json"
logging.basicConfig(
    filename=DATA_DIR / "arctis_control.log", level=logging.INFO,
    format="%(asctime)s %(message)s",
)


def query(dev, cmd):
    """Send 0x06 <cmd> and return the matching reply, skipping replies meant for
    other apps on the same interface (SteelSeries GG polls it too)."""
    dev.write([0x06, cmd] + [0] * 29)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        r = dev.read(8, 250)
        if r[:2] == [0x06, cmd]:
            return r
    return []


def read_headset():
    """Return (connected, battery%) or (None, None) if the dongle isn't plugged in."""
    path = next(
        (d["path"] for d in hid.enumerate(VID, PID)
         if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE),
        None,
    )
    if path is None:
        return None, None
    dev = hid.device()
    try:
        dev.open_path(path)
        status = query(dev, 0x14)                   # connection status
        battery = query(dev, 0x18)                  # battery level
    finally:
        dev.close()
    connected = len(status) > 2 and status[2] == 0x03
    level = min(battery[2], 100) if len(battery) > 2 else None
    return connected, level


def _open_headset():
    path = next(
        (d["path"] for d in hid.enumerate(VID, PID)
         if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE),
        None,
    )
    if path is None:
        return None
    dev = hid.device()
    dev.open_path(path)
    return dev


def headset_status():
    """Cheap on/off check for the 2 s poll: True/False, None if the dongle is missing."""
    dev = _open_headset()
    if dev is None:
        return None
    try:
        status = query(dev, 0x14)
    finally:
        dev.close()
    return len(status) > 2 and status[2] == 0x03


def set_inactive_time(minutes):
    """Headset firmware auto-off after `minutes` without sound (0 = never), as
    HeadsetControl does for the Arctis 7. Stored in the headset."""
    dev = _open_headset()
    if dev is None:
        return False
    try:
        dev.write([0x06, 0x51, minutes] + [0] * 29)
    finally:
        dev.close()
    return True


def cable_connected():
    """The battery reply has no charging flag, but the headset enumerates over USB
    (PID 0x12AE) whenever its charging cable is plugged into the PC."""
    return bool(hid.enumerate(VID, CABLE_PID))


def load_history():
    """[(unix_time, level, charging)] for the last HISTORY_DAYS, oldest first.
    charging is None for rows logged before cable detection existed."""
    cutoff = time.time() - HISTORY_DAYS * 86400
    rows = []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(",")
                try:
                    t = datetime.fromisoformat(parts[0]).timestamp()
                    if t >= cutoff:
                        flag = parts[2] if len(parts) > 2 else ""
                        rows.append((t, int(parts[1]),
                                     {"1": True, "0": False}.get(flag)))
                except (ValueError, IndexError):
                    continue
    except FileNotFoundError:
        pass
    return rows


def unplug_drops(rows):
    """How far the reading falls in the minutes after the cable comes out. The gauge
    is voltage-based, so a big drop means the cell sags hard without the charger."""
    drops = []
    for i in range(1, len(rows)):
        t0, l0, c0 = rows[i - 1]
        t1, _, c1 = rows[i]
        if c0 is True and c1 is False and t1 - t0 <= GAP_SECONDS:
            after = [l for t, l, c in rows[i:] if c is False and t - t1 <= UNPLUG_SETTLE]
            drops.append(l0 - min(after))
    return drops


def estimate_health(rows):
    """Split readings into discharge stretches (broken by gaps or charging), then
    measure the steady drain rate. Sudden big drops are counted as sags and kept
    out of the rate so one charger-unplug settle doesn't skew it."""
    drops = unplug_drops(rows)
    unplugged_at, was_charging = None, None
    stretches, cur, low = [], [], None
    for t, lvl, charging in rows:
        if charging:
            was_charging = True
            if cur:
                stretches.append(cur)
                cur = []
            continue
        if was_charging and charging is False:
            unplugged_at = t
        was_charging = charging
        if unplugged_at and t - unplugged_at < UNPLUG_SETTLE:
            continue                                # still settling after unplug
        if cur and (t - cur[-1][0] > GAP_SECONDS or lvl >= low + CHARGE_RISE):
            stretches.append(cur)
            cur = []
        if not cur:
            low = lvl
        cur.append((t, lvl, charging))
        low = min(low, lvl)
    if cur:
        stretches.append(cur)

    hours = drop = 0.0
    sags = 0
    for st in stretches:
        sag_total = 0
        for (_, l0, _), (_, l1, _) in zip(st, st[1:]):
            if l0 - l1 >= SAG_DROP:
                sags += 1
                sag_total += l0 - l1
        net = st[0][1] - st[-1][1] - sag_total
        if net > 0:
            hours += (st[-1][0] - st[0][0]) / 3600
            drop += net

    h = {"hours": hours, "drop": drop, "sags": sags,
         "unplug_drop": sum(drops[-5:]) / len(drops[-5:]) if drops else None,
         "unplugs": len(drops)}
    if hours < MIN_HOURS or drop < MIN_DROP:
        return h
    h["rate"] = drop / hours                        # % per hour
    h["runtime"] = 100 / h["rate"]                  # hours per full charge
    rt = h["runtime"]
    h["verdict"] = "good" if rt >= 18 and sags < 3 else "failing" if rt < 10 else "worn"
    return h


def health_text(h):
    unplug = (f", drops ~{h['unplug_drop']:.0f}% when unplugged"
              if h.get("unplug_drop") is not None else "")
    if "runtime" not in h:
        return (f"Health: collecting data ({h['hours']:.1f} of {MIN_HOURS} h "
                f"in-use time logged){unplug}")
    sag = f", {h['sags']} sudden drops" if h["sags"] else ""
    return (f"Health: {h['verdict']}, ~{h['runtime']:.0f} h per charge "
            f"(rated {RATED_HOURS} h){sag}{unplug}")


def battery_glyph(level, charging):
    """Segoe Fluent battery icons: E850-E859 + E83F (full), charging E85A-E863 + E83E."""
    n = max(0, min(10, round((level or 0) / 10)))
    if charging:
        return "\uE83E" if n == 10 else chr(0xE85A + n)
    return "\uE83F" if n == 10 else chr(0xE850 + n)


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# handles are pointer-sized; the ctypes default (int) would truncate them
kernel32.CreateEventW.restype = kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
# Without argtypes, HWND_TOPMOST (-1) is passed as a 32-bit int, which 64-bit Windows
# rejects as an invalid handle, so the re-raise above the taskbar silently fails.
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
HWND_TOPMOST = -1
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0x0000
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
OVERLAY_SECONDS = 6
OVERLAY_STYLES = {  # accent colour, Segoe Fluent glyph
    "low": ("#f5a524", "\uE7F6"), "critical": ("#e5484d", "\uE7F6"),
    "on": ("#2ecc71", "\uE7F6"), "off": ("#8a8a8a", "\uEB5E"),
    "charging": ("#6ccb5f", "\uE945"), "unplugged": ("#8a8a8a", "\uE850"),
    "full": ("#6ccb5f", "\uE83F"),
}

BADGE_W, BADGE_H = 104, 40
KEY = "#1c1c1d"       # see-through colour; near the dark taskbar so text edges blend in
HOVER = "#2f2f2f"
LOW_BG = "#c42b1c"    # whole badge goes red at <=20%: visible even with Do Not Disturb on
CLOCK_W = 110         # secondary taskbar clock + notification bell at the right end (96 dpi)
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitors():
    """[(monitor_rect, work_rect, is_primary)] as (left, top, right, bottom) tuples."""
    found = []

    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        found.append(((m.left, m.top, m.right, m.bottom),
                      (w.left, w.top, w.right, w.bottom), bool(mi.dwFlags & 1)))
        return 1

    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    return found


def tray_left():
    """Left edge of the main taskbar's system tray (badge sits just left of it)."""
    user32.FindWindowExW.restype = wintypes.HWND
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    notify = user32.FindWindowExW(tray, None, "TrayNotifyWnd", None) if tray else None
    r = wintypes.RECT()
    if notify and user32.GetWindowRect(notify, ctypes.byref(r)):
        return r.left
    return None


def badge_monitor():
    """Monitor chosen under right-click > Position; default the first non-main screen."""
    mons = monitor_list()
    return (next((m for m in mons if m["name"] == SETTINGS.get("badge_monitor")), None)
            or next((m for m in mons if not m["primary"]), mons[0]))


def badge_position():
    """Bottom-right of the chosen screen inside its taskbar: left of the tray on the
    main screen, left of the clock on others; above the edge if there's no taskbar."""
    m = badge_monitor()
    mon, work = m["rect"], m["work"]
    bar = mon[3] - work[3]
    y = work[3] + (bar - BADGE_H) // 2 if bar >= BADGE_H else mon[3] - BADGE_H - 8
    if bar < BADGE_H:
        x = mon[2] - BADGE_W - 8
    elif m["primary"]:
        x = (tray_left() or mon[2] - 300) - BADGE_W - 4
    else:
        x = mon[2] - BADGE_W - CLOCK_W
    return x, y, mon


def foreground_monitor():
    """Monitor rect of the active window (the game), else the main screen."""
    hwnd = user32.GetForegroundWindow()
    r = wintypes.RECT()
    mons = monitors()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(r)):
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        for mon, _, _ in mons:
            if mon[0] <= cx < mon[2] and mon[1] <= cy < mon[3]:
                return mon
    return next(m[0] for m in mons if m[2])


def fullscreen_on(mon):
    """True if the foreground window covers the whole monitor (game, video)."""
    hwnd = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if not hwnd or cls.value in SHELL_CLASSES:
        return False
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left <= mon[0] and r.top <= mon[1] and r.right >= mon[2] and r.bottom >= mon[3]


def toast(title, msg, loud=False):
    n = Notification(app_id=APP_ID, title=title, msg=msg,
                     duration="long" if loud else "short")
    n.set_audio(audio.Reminder if loud else audio.Default, loop=False)
    n.show()


class Monitor:
    def __init__(self):
        self.level = None
        self.connected = None
        self.charging = None
        self.armed = set(THRESHOLDS)
        self.last_raw = None
        self.health = estimate_health(load_history())
        try:
            self.state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.state = {}
        self.last_level = self.state.get("last_level")
        if self.last_level is None:
            rows = load_history()
            if rows:
                self.last_level = rows[-1][1]
        self.wake = threading.Event()
        self.overlays = queue.SimpleQueue()          # (title, subtitle, style) for the UI thread
        self.turn_off_pending = False                # set from the UI thread
        self.off_requested_at = None                 # monotonic time the 1-min timer was armed

    def status_text(self):
        if self.connected is None:
            return "Checking…" if self.last_raw is None else "Dongle not found"
        plug = "Charging" if self.charging else "Not charging"
        if not self.connected:
            last = f" · last {self.last_level}%" if self.last_level is not None else ""
            return f"Headset off{last} · {plug}"
        left = ""
        if self.health.get("rate") and not self.charging:
            left = f", ~{self.level / self.health['rate']:.0f} h left"
        return f"Battery {self.level}%{left} · {plug}"

    def popup(self, key, title, subtitle, style):
        """Queue a Steam-style popup if that event is switched on in Settings."""
        if SETTINGS["popups"].get(key):
            self.overlays.put((title, subtitle, style))

    def apply_auto_off(self):
        """Keep the headset's own idle timer at never; this app decides when to turn off."""
        if SETTINGS.get("auto_off_applied") or self.off_requested_at:
            return
        try:
            if set_inactive_time(0):
                SETTINGS["auto_off_applied"] = True
                save_settings(SETTINGS)
                logging.info("headset idle timer set to never")
        except Exception as e:
            logging.warning("idle timer write failed: %s", e)

    def request_turn_off(self):
        self.turn_off_pending = True
        self.wake.set()

    def handle_turn_off(self):
        """No 'power off now' command is known for the Arctis 7, so arm its own idle
        timer at 1 minute; it's set back to never on the next connect. If it's still
        on after 3 minutes (sound kept it awake), cancel."""
        if self.turn_off_pending:
            self.turn_off_pending = False
            if self.connected and set_inactive_time(1):
                self.off_requested_at = time.monotonic()
                SETTINGS["auto_off_applied"] = False
                save_settings(SETTINGS)
                logging.info("turn-off requested (idle timer 1 min)")
                self.overlays.put(("Turning off headset", "Arctis 7 · switches off within a minute",
                                   "off"))
        if self.off_requested_at:
            if not self.connected:
                self.off_requested_at = None          # it switched off; restore on reconnect
            elif time.monotonic() - self.off_requested_at > 180:
                self.off_requested_at = None
                logging.info("turn-off did not happen in 3 min, timer back to never")
                self.apply_auto_off()

    def show_health(self):
        h = self.health
        if "runtime" not in h:
            toast("Arctis 7 battery health",
                  f"Still collecting data: {h['hours']:.1f} of {MIN_HOURS} h of in-use time "
                  "logged. Keep the app running while you use the headset."
                  + self.unplug_note())
            return
        toast(f"Arctis 7 battery health: {h['verdict']}",
              f"Drains ~{h['rate']:.1f}%/h, so a full charge lasts ~{h['runtime']:.0f} h "
              f"(rated {RATED_HOURS} h). {h['sags']} sudden drops of {SAG_DROP}%+ "
              f"in the last {HISTORY_DAYS} days, from {h['hours']:.0f} h of use."
              + self.unplug_note())

    def unplug_note(self):
        d = self.health.get("unplug_drop")
        if d is None:
            return ""
        return (f" Reading falls ~{d:.0f}% right after unplugging "
                f"(avg of last {min(self.health['unplugs'], 5)}); a healthy cell drops a few %.")

    def record(self, level):
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')},{level},"
                    f"{int(bool(self.charging))}\n")
        self.health = estimate_health(load_history())
        verdict = self.health.get("verdict")
        if verdict in ("worn", "failing") and self.state.get("warned") != verdict:
            self.state["warned"] = verdict
            STATE_FILE.write_text(json.dumps(self.state), encoding="utf-8")
            self.show_health()

    def update(self):
        try:
            connected, level = read_headset()
        except Exception as e:                      # device yanked mid-read, etc.
            logging.warning("read failed: %s", e)
            connected, level = None, None

        if (connected, level, self.charging) != self.last_raw:
            logging.info("connected=%s level=%s charging=%s", connected, level, self.charging)
            self.last_raw = (connected, level, self.charging)

        prev_connected, prev_level = self.connected, self.level
        self.connected = connected
        self.level = level if connected else None

        if connected and prev_connected is False:
            self.popup("connected", "Headset connected", f"Arctis 7 · {level}%", "on")
        if prev_connected and connected is False:
            self.popup("disconnected", "Headset turned off",
                       f"Arctis 7 · last {prev_level}%" if prev_level is not None else "Arctis 7",
                       "off")
        if connected:
            self.apply_auto_off()

        if connected and level is not None:
            if self.last_level != level:
                self.last_level = level
                self.state["last_level"] = level
                STATE_FILE.write_text(json.dumps(self.state), encoding="utf-8")
            self.record(level)
            self.check_alerts(level, prev_level)

    def check_alerts(self, level, prev_level):
        for t in THRESHOLDS:
            if level > t + REARM_MARGIN:
                self.armed.add(t)
        due = [t for t in THRESHOLDS if t in self.armed and level <= t]
        if due and not self.charging:
            self.armed -= set(due)
            critical = level <= 10
            self.popup(f"low{min(due)}", "Headset battery " + ("critical" if critical else "low"),
                       f"Arctis 7 · {level}% left", "critical" if critical else "low")
            toast("Arctis 7 battery low" if not critical else "Arctis 7 battery critical",
                  f"{level}% left. Plug it in soon." if not critical
                  else f"{level}% left. It will shut off shortly.", loud=True)
        if level == 100 and prev_level is not None and prev_level < 100:
            self.popup("full", "Headset fully charged", "Arctis 7 · 100%", "full")

    def loop(self):
        """Every CABLE_POLL s: charging cable + headset on/off (either changing triggers
        an immediate full read, so the badge flips within ~2 s); battery every POLL_SECONDS."""
        next_read = 0
        while True:
            try:
                cable = cable_connected()
            except Exception:
                cable = self.charging
            try:
                online = headset_status()
            except Exception:
                online = self.connected
            if (cable != self.charging or online != self.connected or self.wake.is_set()
                    or time.monotonic() >= next_read):
                self.wake.clear()
                if self.charging is not None and cable != self.charging:
                    self.popup("charging" if cable else "unplugged",
                               "Charger plugged in" if cable else "Charger unplugged",
                               "Arctis 7", "charging" if cable else "unplugged")
                self.charging = cable
                self.update()
                next_read = time.monotonic() + POLL_SECONDS
            try:
                self.handle_turn_off()
            except Exception as e:
                logging.warning("turn-off failed: %s", e)
            self.wake.wait(CABLE_POLL)


class Badge:
    """Borderless always-on-top badge: headphone glyph + battery icon (charging or
    not) + battery %."""

    def __init__(self, monitor):
        self.m = monitor
        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.config(bg=KEY)
        root.attributes("-transparentcolor", KEY)

        families = set(tkfont.families())
        icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        text_font = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"

        c = self.canvas = tk.Canvas(root, width=BADGE_W, height=BADGE_H, bg=KEY,
                                    highlightthickness=0, cursor="hand2")
        c.pack()
        self.base = KEY
        self.bg = c.create_polygon(self._rounded(1, 1, BADGE_W - 1, BADGE_H - 1, 8),
                                   smooth=True, fill=KEY)
        self.glyph = c.create_text(16, BADGE_H // 2, fill="white",
                                   text="\uE7F6" if icon_font else "🎧",
                                   font=(icon_font or text_font, 13))
        # headset off: small x on the headphone glyph
        self.xdot = c.create_oval(19, 21, 31, 33, fill="#202020", outline="", state="hidden")
        self.xmark = c.create_text(25, 27, fill="white", state="hidden",
                                   text="\uE711" if icon_font else "x",
                                   font=(icon_font or text_font, 6 if icon_font else 7, "bold"))
        self.batt = c.create_text(42, BADGE_H // 2, fill="white", text="",
                                  font=(icon_font or text_font, 15))
        self.label = c.create_text(58, BADGE_H // 2 + 1, anchor="w", text="…",
                                   fill="white", font=(text_font, 11))
        c.bind("<Enter>", self._enter)
        c.bind("<Leave>", self._leave)
        c.bind("<Button-1>", lambda e: self.toggle_mixer())
        c.bind("<Button-3>", self._menu)
        self.tip = None
        self.menu_open = False
        self.update_info = None
        self.icon_font, self.text_font = icon_font, text_font
        self.shown = None
        self.hidden = False

        self.show_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)
        root.after(300, self._poll_show)
        self.mixer = Mixer()
        self.dts = DTSController()
        self.panel = MixerPanel(root, self.mixer, self.dts, icon_font, text_font)
        self.panel.on_power = self.m.request_turn_off
        self.panel.is_connected = lambda: bool(self.m.connected)
        threading.Thread(target=self._update_loop, daemon=True).start()
        self.meters, self.quiet_since = {}, None
        root.after(2000, self.watch_activity)
        root.after(500, self.sweep)

        root.update_idletasks()
        self.hwnd = int(root.wm_frame(), 16)
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.place()
        self.refresh()
        self.hook_foreground()

    def _poll_show(self):
        """A Start menu / taskbar launch while we're running signals SHOW_EVENT."""
        if kernel32.WaitForSingleObject(self.show_event, 0) == 0:
            self.show_mixer()
        self.root.after(300, self._poll_show)

    def show_mixer(self):
        if self.panel.win:
            self.panel.win.lift()
            self.panel.win.focus_force()
        else:
            self.toggle_mixer()

    def toggle_mixer(self):
        self._leave(None)
        self.panel.toggle(self.root.winfo_rootx() + BADGE_W, self.root.winfo_rooty())

    def sweep(self):
        try:
            self.mixer.sweep()
        except Exception as e:
            logging.warning("mixer sweep failed: %s", e)
        self.root.after(SWEEP_MS, self.sweep)

    def activity_level(self):
        """Loudest peak right now on the headset's outputs and its mic (0..1)."""
        peak = 0.0
        for d in self.mixer.outputs + self.mixer.inputs:
            if "Arctis 7" not in (d.FriendlyName or ""):
                continue
            try:
                meter = self.meters.get(d.id)
                if meter is None:
                    meter = self.meters[d.id] = d._dev.Activate(
                        IAudioMeterInformation._iid_, CLSCTX_ALL, None
                    ).QueryInterface(IAudioMeterInformation)
                peak = max(peak, meter.GetPeakValue())
            except Exception:
                self.meters.pop(d.id, None)
        return peak

    def watch_activity(self):
        """Auto-off: no sound out and nothing on the mic for N minutes -> turn off."""
        try:
            if not (SETTINGS.get("auto_off") and self.m.connected) or self.m.off_requested_at:
                self.quiet_since = None
            elif self.activity_level() > 0.001:
                self.quiet_since = None
            elif self.quiet_since is None:
                self.quiet_since = time.monotonic()
            elif time.monotonic() - self.quiet_since > SETTINGS["auto_off_minutes"] * 60:
                self.quiet_since = None
                logging.info("no activity for %s min, turning headset off",
                             SETTINGS["auto_off_minutes"])
                self.m.request_turn_off()
        except Exception as e:
            logging.warning("activity check failed: %s", e)
        self.root.after(2000, self.watch_activity)

    @staticmethod
    def _rounded(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _update_loop(self):
        """Once a day (first check a minute after start): newer GitHub release -> one popup."""
        time.sleep(60)
        while True:
            if SETTINGS.get("check_updates", True):
                try:
                    info = updater.check()
                    if info["newer"]:
                        self.update_info = info
                        if self.m.state.get("update_notified") != info["tag"]:
                            self.m.state["update_notified"] = info["tag"]
                            STATE_FILE.write_text(json.dumps(self.m.state), encoding="utf-8")
                            self.m.popup("update", "Update available",
                                         f"Arctis Control {info['tag']} · Settings > Update now", "on")
                except Exception as e:
                    logging.info("update check failed: %s", e)
            time.sleep(24 * 3600)

    def raise_now(self):
        if not self.hidden and not self.menu_open:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def hook_foreground(self):
        """Clicking the Windows 11 taskbar brings it in front of every topmost window.
        Re-raise the badge the moment anything becomes foreground (not just on the 1 s
        tick), plus a few follow-ups while the taskbar settles."""
        self._last_foreground = 0.0

        def on_event(_hook, _event, _hwnd, _obj, _child, _thread, _time):
            # Runs inside Tk's event loop: calling Tk from here is a fatal GIL error,
            # so only raw Win32 + a timestamp; follow-ups run in _raise_burst.
            self._last_foreground = time.monotonic()
            self.raise_now()

        self._win_event_proc = WINEVENTPROC(on_event)     # keep a reference: no GC
        self._win_event_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
            self._win_event_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        self.root.after(100, self._raise_burst)

    def _raise_burst(self):
        if time.monotonic() - self._last_foreground < 0.6:   # taskbar may re-raise itself
            self.raise_now()
        self.root.after(100, self._raise_burst)

    def place(self):
        """Every second: follow monitor changes, stay above the taskbar (it re-raises
        itself when clicked) and step aside for fullscreen apps on that screen."""
        x, y, mon = badge_position()
        if fullscreen_on(mon):
            if not self.hidden:
                self.root.withdraw()
                self.hidden = True
        else:
            if self.hidden:
                self.root.deiconify()
                self.hidden = False
            self.root.geometry(f"{BADGE_W}x{BADGE_H}+{x}+{y}")   # "+-188": a bare "-188" means "from the right edge"
            self.raise_now()
        self.root.after(1000, self.place)

    def refresh(self):
        state = (self.m.connected, self.m.level, self.m.charging)
        if state != self.shown:
            self.shown = state
            connected, level, charging = state
            online = connected and level is not None
            shown_level = level if online else self.m.last_level
            if self.icon_font:
                # headset off / out of range: headphone glyph becomes "wireless disconnected"
                self.canvas.itemconfig(self.glyph, text="\uE7F6", fill="white")
                self.canvas.itemconfig(
                    self.batt, fill="#6ccb5f" if charging else "white",
                    text=battery_glyph(shown_level, charging)
                    if shown_level is not None or charging else "")
            else:
                self.canvas.itemconfig(self.glyph, text="🎧" if online else "✕", fill="white")
                self.canvas.itemconfig(self.batt, text="⚡" if charging else "")
            for item in (self.xdot, self.xmark):
                self.canvas.itemconfig(item, state="hidden" if online else "normal")
            if online:
                low = level <= 20 and not charging
                self.base = LOW_BG if low else KEY
                color = "white" if low else "#f2c94c" if level <= 50 else "white"
                self.canvas.itemconfig(self.label, text=f"{level}%", fill=color)
            else:
                self.base = KEY
                self.canvas.itemconfig(self.label, fill="white",
                                       text=f"{shown_level}%" if shown_level is not None else "–")
            if not self.tip:
                self.canvas.itemconfig(self.bg, fill=self.base)
        while not self.m.overlays.empty():
            self.overlay(*self.m.overlays.get())
        self.root.after(1000, self.refresh)

    def overlay(self, title, subtitle, style, mon=None):
        """Steam-style popup: bottom-right of the screen being played on, click-through,
        never activated, fades out. Shows over windowed/borderless games; exclusive
        fullscreen bypasses the desktop, so nothing outside the game can draw there."""
        if mon is None:
            target = SETTINGS.get("popup_monitor", "active")
            mon = (None if target == "active" else (find_monitor(target) or {}).get("rect")) \
                or foreground_monitor()
        w, h = 300, 66
        accent, glyph = OVERLAY_STYLES.get(style, OVERLAY_STYLES["low"])
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.config(bg="#1d1f23")
        win.geometry(f"{w}x{h}+{mon[2] - w - 24}+{mon[3] - h - 64}")
        tk.Frame(win, bg=accent, width=5).pack(side="left", fill="y")
        tk.Label(win, text=glyph if self.icon_font else "🎧", fg=accent, bg="#1d1f23",
                 font=(self.icon_font or self.text_font, 20)).pack(side="left", padx=(14, 10))
        body = tk.Frame(win, bg="#1d1f23")
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text=title, fg="white", bg="#1d1f23", anchor="w",
                 font=(self.text_font, 11, "bold")).pack(fill="x", pady=(12, 0))
        tk.Label(body, text=subtitle, fg="#a8adb7", bg="#1d1f23", anchor="w",
                 font=(self.text_font, 9)).pack(fill="x")
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                              | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

        def fade(alpha, step, then=None):
            if not win.winfo_exists():
                return
            alpha = max(0.0, min(0.95, alpha + step))
            win.attributes("-alpha", alpha)
            if (step > 0 and alpha < 0.95) or (step < 0 and alpha > 0):
                win.after(16, fade, alpha, step, then)
            elif then:
                then()

        fade(0.0, 0.08, lambda: win.after(OVERLAY_SECONDS * 1000,
                                         lambda: fade(0.95, -0.06, win.destroy)))

    def _enter(self, _e):
        if self.base == KEY:
            self.canvas.itemconfig(self.bg, fill=HOVER)
        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        if self.dts.available:
            dts_line = f"\nDTS: {self.dts.preset_label}" if self.dts.enabled else "\nDTS: Off"
        else:
            dts_line = ""
        tk.Label(self.tip, text=f"Arctis 7\n{self.m.status_text()}\n{health_text(self.m.health)}{dts_line}",
                 justify="left", bg="#2b2b2b", fg="white", padx=10, pady=6,
                 font=("Segoe UI", 9)).pack()
        self.tip.update_idletasks()
        x = self.root.winfo_rootx() + BADGE_W - self.tip.winfo_width()   # right-aligned
        y = self.root.winfo_rooty() - self.tip.winfo_height() - 6
        self.tip.geometry(f"+{x}+{y}")

    def _leave(self, _e):
        self.canvas.itemconfig(self.bg, fill=self.base)
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _menu(self, e):
        self._leave(e)
        menu = tk.Menu(self.root, tearoff=0)
        position = tk.Menu(menu, tearoff=0)
        self._pos_var = tk.StringVar(value=badge_monitor()["name"])   # kept alive on self
        for m in monitor_list():
            position.add_radiobutton(label=m["label"], value=m["name"], variable=self._pos_var,
                                     command=lambda n=m["name"]: self.set_badge_monitor(n))
        menu.add_cascade(label="Position", menu=position)
        menu.add_command(label="Battery health", command=self.show_health_panel)
        menu.add_command(label="Settings…", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.root.destroy)
        # A popup menu only closes on an outside click if its owner is the foreground
        # window, and the badge is WS_EX_NOACTIVATE so clicks never make it one.
        # Activating it explicitly (allowed right after the user's click) fixes that.
        self.menu_open = True              # stop re-raising the badge over the menu
        user32.SetForegroundWindow(self.hwnd)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
            self.root.after(300, lambda: setattr(self, "menu_open", False))

    def set_badge_monitor(self, name):
        SETTINGS["badge_monitor"] = name
        save_settings(SETTINGS)              # place() moves the badge within a second

    def open_settings(self):
        if getattr(self, "settings_win", None) and self.settings_win.win.winfo_exists():
            self.settings_win.win.lift()
            self.settings_win.win.focus_force()
            return
        level = self.m.level if self.m.level is not None else self.m.last_level
        self.settings_win = SettingsWindow(
            self.root, SETTINGS,
            test_popup=lambda mon: self.overlay("Headset battery low",
                                                f"Arctis 7 · {level}% left (test)", "low",
                                                mon=mon or foreground_monitor()),
            icon=str(RES_DIR / "arctis_control.ico"), on_save=self.m.wake.set,
            on_quit=self.root.destroy)

    def show_health_panel(self):
        h = self.m.health
        rows = [("Verdict", h.get("verdict", "collecting data").capitalize())]
        if "runtime" in h:
            rows += [("Per full charge", f"~{h['runtime']:.0f} h (rated {RATED_HOURS} h)"),
                     ("Drain while in use", f"{h['rate']:.1f}% per hour")]
        else:
            rows.append(("Data so far", f"{h['hours']:.1f} of {MIN_HOURS} h of use logged"))
        rows.append(("Sudden drops", f"{h['sags']} in the last {HISTORY_DAYS} days"))
        if h.get("unplug_drop") is not None:
            rows.append(("Drop after unplugging",
                         f"~{h['unplug_drop']:.0f}% (healthy: a few %)"))
        rows.append(("Now", self.m.status_text()))
        HealthPanel(self.root, self.root.winfo_rootx() + BADGE_W, self.root.winfo_rooty(), rows)

    def run(self):
        threading.Thread(target=self.m.loop, daemon=True).start()
        self.root.mainloop()


def register_app_id():
    """Name + icon Windows shows on our toasts (no installer/Start-menu AUMID needed)."""
    import winreg
    png = DATA_DIR / "arctis_control.png"      # stable path outside the exe's temp/bundle dir
    if not png.exists():
        import shutil
        shutil.copy(RES_DIR / "arctis_control.png", png)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          rf"Software\Classes\AppUserModelId\{APP_ID}") as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(png))


if __name__ == "__main__":
    background = "--background" in sys.argv          # login start: no window
    kernel32.CreateMutexW(None, False, "ArctisBatteryMonitor")
    if kernel32.GetLastError() == 183:                 # ERROR_ALREADY_EXISTS
        if not background:                             # opened from Start/taskbar: show mixer
            ev = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)   # EVENT_MODIFY_STATE
            if ev:
                kernel32.SetEvent(ev)
                kernel32.CloseHandle(ev)
        raise SystemExit
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    register_app_id()
    ctypes.windll.shcore.SetProcessDpiAwareness(2)     # real pixels for positioning
    badge = Badge(Monitor())
    if not background:
        badge.root.after(1200, badge.show_mixer)
    badge.run()
