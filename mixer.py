"""
Mini Game / Chat / Media / Mic mixer: a light stand-in for SteelSeries Sonar.

Volumes: Game, Chat and Media are per-app volumes (apps sorted by exe name,
editable in mixer_apps.json; anything unlisted counts as Game). Mic is the
chosen microphone's own input level.

Devices (Sonar-style pickers, channels may share a device):
  Game  -> Windows default output; game apps follow it (stale pins are cleared)
  Chat  -> chat apps pinned to it, and it becomes the voice-chat default
  Media -> media apps pinned to it
  Mic   -> Windows default recording device
Pins use Windows' per-app device setting (app_routing.py), which applies to
streams an app opens afterwards; most modern apps switch on the fly.

Runs on the Tk thread: one sweep every 2 s, a few ms each.
"""
import ctypes
import json
import math
import os
import tkinter as tk
from pathlib import Path

import psutil
from pycaw.constants import ERole
from comtypes import CLSCTX_ALL
from pycaw.pycaw import (AudioUtilities, IAudioMeterInformation, IAudioSessionControl2,
                         ISimpleAudioVolume)

import app_routing
from pycaw.api.audioclient import IAudioClient

from paths import DATA_DIR
APPS_FILE = DATA_DIR / "mixer_apps.json"
STATE_FILE = DATA_DIR / "mixer_state.json"
SWEEP_MS = 2000
METER_MS = 16                         # level meters redraw at ~60 fps, only while the panel is open
METER_FLOOR_DB = -60                  # OBS-style dB scale: -60 dB .. 0 dB
METER_FALL = 0.01                     # falloff per frame (fraction of the bar)
DEVICE_REFRESH = 5                    # sweeps between re-listing audio devices
HIDDEN_DEVICES = ("SteelSeries Sonar",)

DEFAULT_APPS = {
    "chat": ["discord.exe", "discordptb.exe", "discordcanary.exe", "ts3client_win64.exe",
             "teamspeak.exe", "slack.exe", "zoom.exe", "ms-teams.exe", "skype.exe",
             "whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "mumble.exe"],
    "media": ["spotify.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
              "opera.exe", "opera_gx.exe", "vivaldi.exe", "vlc.exe", "mpc-hc64.exe",
              "mpc-be64.exe", "potplayermini64.exe", "foobar2000.exe", "musicbee.exe",
              "applemusic.exe", "tidal.exe", "deezer.exe", "aimp.exe", "mpv.exe"],
    # never touched: Sonar/Voicemeeter route the final mix, audiodg is Windows' engine
    "ignore": ["steelseriessonar.exe", "audiodg.exe", "voicemeeter.exe",
               "voicemeeterpro.exe", "voicemeeter8.exe", "voicemeeter8x64.exe"],
}

CHANNELS = [  # key, label, glyph, muted glyph (Segoe Fluent Icons)
    ("game", "Game", "", ""),
    ("chat", "Chat", "", ""),
    ("media", "Media", "", ""),
    ("mic", "Microphone", "", ""),
]

BG, BORDER, TRACK, ACCENT, DIM = "#202020", "#3a3a3a", "#4a4a4a", "#4cc2ff", "#8a8a8a"


def load_apps():
    if not APPS_FILE.exists():
        APPS_FILE.write_text(json.dumps(DEFAULT_APPS, indent=2), encoding="utf-8")
    try:
        data = json.loads(APPS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        data = DEFAULT_APPS
    return {k: {n.lower() for n in data.get(k, [])} for k in ("chat", "media", "ignore")}


def short_name(friendly):
    """'Headphones (Arctis 7 Game)' -> 'Arctis 7 Game', 'Speakers (Realtek(R) Audio)' -> 'Speakers'."""
    if "(" not in friendly:
        return friendly
    outer, inner = friendly.split(" (", 1)
    inner = inner[:-1] if inner.endswith(")") else inner
    return inner if "Arctis" in inner else outer


class Mixer:
    def __init__(self):
        self.apps = load_apps()
        self.names = {}                   # pid -> exe name
        self.outputs, self.inputs = [], []
        self.sweeps = 0
        try:
            self.routing = app_routing.AppRouting()
        except OSError:
            self.routing = None           # per-app pins unavailable on this Windows build
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.vol, self.mute = st["vol"], st["mute"]
            self.dev = st.get("dev")
        except (FileNotFoundError, ValueError, KeyError):
            self.vol, self.mute, self.dev = None, {k: False for k, *_ in CHANNELS}, None

    # --- devices ---------------------------------------------------------------
    def refresh_devices(self):
        keep = lambda d: d.FriendlyName and not d.FriendlyName.startswith(HIDDEN_DEVICES)
        self.outputs = [d for d in AudioUtilities.GetAllDevices(data_flow=0, device_state=1) if keep(d)]
        self.inputs = [d for d in AudioUtilities.GetAllDevices(data_flow=1, device_state=1) if keep(d)]

    def device(self, ch):
        """The AudioDevice currently chosen for a channel (None if unplugged)."""
        pool = self.inputs if ch == "mic" else self.outputs
        return next((d for d in pool if d.id == (self.dev or {}).get(ch)), None)

    def choices(self, ch):
        return self.inputs if ch == "mic" else self.outputs

    @staticmethod
    def default_id(flow, role):
        try:
            return AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(
                flow, getattr(role, "value", role)).GetId()
        except Exception:
            return None

    def set_device(self, ch, dev_id):
        self.dev[ch] = dev_id
        self.save()
        if ch == "game":
            AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eConsole, ERole.eMultimedia])
        elif ch == "chat":
            AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eCommunications])
        elif ch == "mic":
            AudioUtilities.SetDefaultDevice(
                dev_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        self.route_all()

    # --- sessions --------------------------------------------------------------
    def channel_of(self, pid):
        if pid in (0, os.getpid()):
            return None
        name = self.names.get(pid)
        if name is None:
            try:
                name = psutil.Process(pid).name().lower()
            except psutil.Error:
                return None
            self.names[pid] = name
        if name in self.apps["ignore"]:
            return None
        if name in self.apps["chat"]:
            return "chat"
        return "media" if name in self.apps["media"] else "game"

    def sessions(self):
        """(channel, pid, ISimpleAudioVolume) for every app session on every active output."""
        for dev in self.outputs:
            try:
                en = dev.AudioSessionManager.GetSessionEnumerator()
                for i in range(en.GetCount()):
                    ctl = en.GetSession(i).QueryInterface(IAudioSessionControl2)
                    pid = ctl.GetProcessId()
                    ch = self.channel_of(pid)
                    if ch:
                        yield ch, pid, ctl.QueryInterface(ISimpleAudioVolume)
            except Exception:             # device unplugged mid-sweep
                continue

    def route(self, ch, pid):
        """Pin chat/media apps to their channel's device; game apps follow the default."""
        if not self.routing:
            return
        want = self.dev.get(ch) if ch in ("chat", "media") else None
        if ch in ("chat", "media") and not self.device(ch):
            return                        # chosen device unplugged: leave the app alone
        try:
            if self.routing.get(pid, app_routing.RENDER) != want:
                self.routing.set(pid, app_routing.RENDER, want)
        except OSError:
            pass

    def route_all(self):
        for ch, pid in {(ch, pid) for ch, pid, _ in self.sessions()}:
            self.route(ch, pid)

    def adopt(self):
        """First run: take today's levels and devices as the starting point, no jumps."""
        if self.vol is None:
            self.vol = {"game": 100, "chat": 100, "media": 100}
            seen = set()
            for ch, _, sv in self.sessions():
                if ch not in seen:
                    self.vol[ch] = round(sv.GetMasterVolume() * 100)
                    seen.add(ch)
            self.vol["mic"] = 100
        if self.dev is None:
            game = self.default_id(0, ERole.eMultimedia)
            self.dev = {"game": game, "chat": self.default_id(0, ERole.eCommunications),
                        "media": game, "mic": self.default_id(1, ERole.eCommunications)}
            if self.device("mic"):
                self.vol["mic"] = self.mic_level()
        self.save()

    def sweep(self):
        """Re-apply channel levels and device pins to sessions that drifted (new app,
        app reset itself, device replugged)."""
        if self.sweeps % DEVICE_REFRESH == 0:
            self.refresh_devices()
            self.names.clear()
        self.sweeps += 1
        if self.vol is None or self.dev is None:
            self.adopt()
        routed = set()
        for ch, pid, sv in self.sessions():
            target = self.vol[ch] / 100
            if abs(sv.GetMasterVolume() - target) > 0.004:
                sv.SetMasterVolume(target, None)
            if bool(sv.GetMute()) != self.mute[ch]:
                sv.SetMute(self.mute[ch], None)
            if pid not in routed:         # an app has a session on every device it touched
                routed.add(pid)
                self.route(ch, pid)

    def channel_meters(self):
        """{channel: [IAudioMeterInformation]} for the level bars in the panel."""
        out = {"game": [], "chat": [], "media": [], "mic": []}
        for dev in self.outputs:
            try:
                en = dev.AudioSessionManager.GetSessionEnumerator()
                for i in range(en.GetCount()):
                    ctl = en.GetSession(i).QueryInterface(IAudioSessionControl2)
                    ch = self.channel_of(ctl.GetProcessId())
                    if ch:
                        out[ch].append(ctl.QueryInterface(IAudioMeterInformation))
            except Exception:
                continue
        mic = self.device("mic")
        if mic:
            try:
                out["mic"].append(mic._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL,
                                                    None).QueryInterface(IAudioMeterInformation))
            except Exception:
                pass
        return out

    def open_mic_monitor(self):
        """A mic's level meter reads 0 unless some app is recording from it, so while the
        panel is open we hold a tiny shared-mode capture stream (data is never read)."""
        mic = self.device("mic")
        if not mic:
            return None
        client = mic._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
        fmt = client.GetMixFormat()
        try:
            client.Initialize(0, 0, 1_000_000, 0, fmt, None)      # shared, 100 ms buffer
        finally:
            ctypes.windll.ole32.CoTaskMemFree(fmt)
        client.Start()
        return client

    @staticmethod
    def close_mic_monitor(client):
        if client:
            try:
                client.Stop()
            except Exception:
                pass

    # --- levels ----------------------------------------------------------------
    def mic_level(self):
        return round(self.device("mic").EndpointVolume.GetMasterVolumeLevelScalar() * 100)

    def set(self, ch, value=None, mute=None):
        if value is not None:
            self.vol[ch] = value
        if mute is not None:
            self.mute[ch] = mute
        if ch == "mic":
            mic = self.device("mic")
            if mic:
                ev = mic.EndpointVolume
                ev.SetMasterVolumeLevelScalar(self.vol["mic"] / 100, None)
                ev.SetMute(self.mute["mic"], None)
        else:
            for c, _, sv in self.sessions():
                if c == ch:
                    sv.SetMasterVolume(self.vol[ch] / 100, None)
                    sv.SetMute(self.mute[ch], None)

    def sync_mic(self):
        """Mic level can also change in Windows settings or Discord; mirror it."""
        mic = self.device("mic")
        if mic:
            try:
                self.vol["mic"] = self.mic_level()
                self.mute["mic"] = bool(mic.EndpointVolume.GetMute())
            except Exception:
                pass

    def save(self):
        STATE_FILE.write_text(json.dumps({"vol": self.vol, "mute": self.mute, "dev": self.dev}),
                              encoding="utf-8")


class Slider:
    W, H, PAD = 170, 30, 9
    METER_Y = 25                      # thin level bar under the track

    def __init__(self, parent, value, on_change, on_release):
        self.c = tk.Canvas(parent, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.on_change, self.on_release = on_change, on_release
        y = 13
        self.c.create_line(self.PAD, y, self.W - self.PAD, y, fill=TRACK, width=4, capstyle="round")
        my, span = self.METER_Y, self.W - 2 * self.PAD
        self.c.create_rectangle(self.PAD, my, self.W - self.PAD, my + 2, fill="#2e2e2e", width=0)
        self.zones = [(self.PAD + span * a, self.PAD + span * b, color) for a, b, color in
                      ((0, 40 / 60, "#3fbf5f"), (40 / 60, 51 / 60, "#e6c229"), (51 / 60, 1, "#e5484d"))]
        self.segs = [self.c.create_rectangle(x0, my, x0, my + 2, fill=color, width=0,
                                             state="hidden") for x0, _, color in self.zones]
        self.level = 0.0
        # optional second meter (NC processed level): cyan bar at METER_Y + 4
        self.nc_segs = [self.c.create_rectangle(x0, my + 4, x0, my + 6,
                        fill="#40c8e0", width=0, state="hidden") for x0, _, _ in self.zones]
        self.nc_level = 0.0
        self.fill = self.c.create_line(self.PAD, y, self.PAD, y, fill=ACCENT, width=4, capstyle="round")
        self.knob = self.c.create_oval(0, 0, 0, 0, fill="white", outline=ACCENT, width=3)
        self.c.bind("<Button-1>", self._drag)
        self.c.bind("<B1-Motion>", self._drag)
        self.c.bind("<ButtonRelease-1>", lambda e: self.on_release())
        self.c.bind("<MouseWheel>", self._wheel)
        self.value = None
        self.draw(value)

    def draw(self, value, color=ACCENT):
        self.value = max(0, min(100, int(round(value))))
        x = self.PAD + (self.W - 2 * self.PAD) * self.value / 100
        y = 13
        self.c.coords(self.fill, self.PAD, y, x, y)
        self.c.itemconfig(self.fill, fill=color)
        self.c.coords(self.knob, x - 7, y - 7, x + 7, y + 7)
        self.c.itemconfig(self.knob, outline=color)

    def show_level(self, peak):
        """peak 0..1 -> dB scale; rises instantly, falls back smoothly like OBS."""
        db = 20 * math.log10(peak) if peak > 1e-6 else METER_FLOOR_DB
        target = max(0.0, min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))
        level = target if target > self.level else max(target, self.level - METER_FALL)
        if abs(level - self.level) < 0.002:
            return                    # nothing visible changed: skip the redraw
        self.level = level
        x = self.PAD + (self.W - 2 * self.PAD) * level
        for seg, (x0, x1, _) in zip(self.segs, self.zones):
            if x <= x0:               # a zero-width rectangle still draws a 1 px tick
                self.c.itemconfig(seg, state="hidden")
            else:
                self.c.coords(seg, x0, self.METER_Y, min(x, x1), self.METER_Y + 2)
                self.c.itemconfig(seg, state="normal")

    def show_nc_level(self, peak):
        """Second meter bar showing NC-processed level (cyan)."""
        db = 20 * math.log10(peak) if peak > 1e-6 else METER_FLOOR_DB
        target = max(0.0, min(1.0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))
        level = target if target > self.nc_level else max(target, self.nc_level - METER_FALL)
        if abs(level - self.nc_level) < 0.002:
            return
        self.nc_level = level
        my = self.METER_Y + 4
        x = self.PAD + (self.W - 2 * self.PAD) * level
        for seg, (x0, x1, _) in zip(self.nc_segs, self.zones):
            if x <= x0:
                self.c.itemconfig(seg, state="hidden")
            else:
                self.c.coords(seg, x0, my, min(x, x1), my + 2)
                self.c.itemconfig(seg, state="normal")

    def hide_nc_level(self):
        for seg in self.nc_segs:
            self.c.itemconfig(seg, state="hidden")
        self.nc_level = 0.0

    def _drag(self, e):
        v = (e.x - self.PAD) / (self.W - 2 * self.PAD) * 100
        self.on_change(max(0, min(100, int(round(v)))))

    def _wheel(self, e):
        self.on_change(max(0, min(100, self.value + (2 if e.delta > 0 else -2))))
        self.on_release()


class MixerPanel:
    """Popup above the battery badge. Closes on Esc, on clicking elsewhere, or on
    clicking the badge again."""

    def __init__(self, root, mixer, dts, icon_font, text_font, nc=None):
        self.root, self.m, self.dts = root, mixer, dts     # dts: dts.DTSController
        self.nc = nc                                        # noise_cancel.NoiseCanceller
        self.icon_font, self.text_font = icon_font, text_font
        self.win = None
        self.menu_open = False

    def toggle(self, anchor_right, anchor_top):
        if self.win:
            self.close()
        else:
            self.open(anchor_right, anchor_top)

    def open(self, anchor_right, anchor_top):
        self.m.refresh_devices()
        self.m.sync_mic()
        win = self.win = tk.Toplevel(self.root, bg=BORDER)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        body = tk.Frame(win, bg=BG, padx=14, pady=10)
        body.pack(padx=1, pady=1)
        tk.Label(body, text="Arctis 7 mixer", bg=BG, fg="white", anchor="w",
                 font=(self.text_font, 10, "bold")).grid(row=0, column=0, columnspan=3,
                                                         sticky="w", pady=(0, 4))
        on_power = getattr(self, "on_power", None)     # set by the badge: turn headset off
        if on_power:
            power = self.power = tk.Frame(body, bg="#2d2d2d", cursor="hand2")
            power.grid(row=0, column=2, sticky="e", pady=(0, 4))
            self._show_power()
            parts = [tk.Label(power, text="Turn off", bg="#2d2d2d", fg="white", padx=6, pady=2,
                              cursor="hand2", font=(self.text_font, 8))]
            if self.icon_font:
                parts.insert(0, tk.Label(power, text="", bg="#2d2d2d", fg="white",
                                         padx=0, pady=2, cursor="hand2",
                                         font=(self.icon_font, 9)))
            for w in [power] + parts:
                w.bind("<Button-1>", lambda e: (self.close(), on_power()))
            for w in parts:
                w.pack(side="left", padx=(6, 0) if w is parts[0] and len(parts) > 1 else 0)
        self.rows = {}
        for i, (key, label, glyph, muted_glyph) in enumerate(CHANNELS):
            r = 1 + i * 2
            icon = tk.Label(body, bg=BG, width=2, cursor="hand2",
                            font=(self.icon_font or self.text_font, 14))
            icon.grid(row=r, column=0, padx=(0, 8), pady=(6, 0))
            text = tk.Label(body, bg=BG, fg="white", anchor="w", width=17,
                            font=(self.text_font, 9))
            text.grid(row=r, column=1, sticky="w", pady=(6, 0))
            slider = Slider(body, self.m.vol[key],
                            lambda v, k=key: self.change(k, v), self.m.save)
            slider.c.grid(row=r, column=2, pady=(6, 0))
            picker = tk.Label(body, bg="#2b2b2b", fg="#c8c8c8", anchor="w", cursor="hand2",
                              padx=8, pady=2, font=(self.text_font, 8))
            picker.grid(row=r + 1, column=1, columnspan=2, sticky="we", pady=(2, 0))
            icon.bind("<Button-1>", lambda e, k=key: self.toggle_mute(k))
            picker.bind("<Button-1>", lambda e, k=key: self.pick(k, e))
            self.rows[key] = (icon, text, slider, picker, label, glyph, muted_glyph)
            self.paint(key)

        self.dts_buttons = {}
        if self.dts and self.dts.available:
            from dts import PRESETS
            r = 2 + len(CHANNELS) * 2
            tk.Label(body, text="" if self.icon_font else "", bg=BG, fg="white",
                     font=(self.icon_font or self.text_font, 14)).grid(row=r, column=0,
                                                                      padx=(0, 8), pady=(12, 0))
            tk.Label(body, text="Surround (DTS)", bg=BG, fg="white", anchor="w",
                     font=(self.text_font, 9)).grid(row=r, column=1, sticky="w", pady=(12, 0))
            seg = tk.Frame(body, bg=BG)
            seg.grid(row=r, column=2, sticky="e", pady=(12, 0))
            for key, label in [("off", "Off")] + [(k, lbl) for k, _, lbl in PRESETS]:
                b = tk.Label(seg, text=label, bg="#2d2d2d", fg="white", padx=7, pady=2,
                             cursor="hand2", font=(self.text_font, 8))
                b.pack(side="left", padx=(2, 0))
                b.bind("<Button-1>", lambda e, k=key: self.pick_dts(k))
                self.dts_buttons[key] = b
            self.paint_dts()

        self.nc_buttons = {}
        self.nc_hint_label = None
        if self.nc and self.nc.available:
            r = 2 + len(CHANNELS) * 2
            if self.dts and self.dts.available:
                r += 1
            tk.Label(body, text="" if self.icon_font else "NC", bg=BG, fg="white",
                     font=(self.icon_font or self.text_font, 14)).grid(row=r, column=0,
                                                                      padx=(0, 8), pady=(12, 0))
            tk.Label(body, text="Noise Cancel", bg=BG, fg="white", anchor="w",
                     font=(self.text_font, 9)).grid(row=r, column=1, sticky="w", pady=(12, 0))
            seg = tk.Frame(body, bg=BG)
            seg.grid(row=r, column=2, sticky="e", pady=(12, 0))
            for key, label in [("off", "Off"), ("on", "On")]:
                b = tk.Label(seg, text=label, bg="#2d2d2d", fg="white", padx=7, pady=2,
                             cursor="hand2", font=(self.text_font, 8))
                b.pack(side="left", padx=(2, 0))
                b.bind("<Button-1>", lambda e, k=key: self.pick_nc(k))
                self.nc_buttons[key] = b
            hint = self.nc.input_device_hint
            if hint:
                self.nc_hint_label = tk.Label(
                    body, text=f"In-game mic: {hint}", bg=BG, fg="#7a7a7a",
                    anchor="w", font=(self.text_font, 7))
                self.nc_hint_label.grid(row=r + 1, column=1, columnspan=2,
                                        sticky="w", pady=(1, 0))
            self.paint_nc()

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{anchor_right - w}+{anchor_top - h - 8}")
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<FocusOut>", lambda e: win.after(150, self._maybe_close))
        win.focus_force()
        self.meter_sources = {}
        self.restart_mic_monitor()
        self._meters()

    def restart_mic_monitor(self):
        self.m.close_mic_monitor(getattr(self, "mic_client", None))
        try:
            self.mic_client = self.m.open_mic_monitor()
        except Exception:
            self.mic_client = None

    def _maybe_close(self):
        if self.win and not self.menu_open and self.win.focus_get() is None:
            self.close()

    def _show_power(self):
        """Turn off only makes sense while the headset is on."""
        power = getattr(self, "power", None)
        if power is not None and power.winfo_exists():
            on = bool(getattr(self, "is_connected", lambda: True)())
            # grid_info(), not winfo_ismapped(): while the panel is still being built nothing
            # is mapped yet, so the button was never hidden until the next check (flash).
            shown = bool(power.grid_info())
            if on and not shown:
                power.grid()
            elif not on and shown:
                power.grid_remove()

    def _meters(self, frame=0):
        if not self.win:
            return                    # panel closed: meters stop, zero cost
        if frame % (SWEEP_MS // METER_MS) == 0:
            self.meter_sources = self.m.channel_meters()
        if frame % 30 == 0:
            self._show_power()
        for key, row in self.rows.items():
            peak = 0.0
            for meter in self.meter_sources.get(key, []):
                try:
                    peak = max(peak, meter.GetPeakValue())
                except Exception:
                    pass
            slider = row[2]
            slider.show_level(0.0 if self.m.mute[key] else peak)
            if key == "mic" and self.nc and self.nc.running:
                slider.show_nc_level(self.nc.processed_peak)
            elif key == "mic":
                slider.hide_nc_level()
        self.win.after(METER_MS, self._meters, frame + 1)

    def close(self):
        if self.win:
            self.m.close_mic_monitor(getattr(self, "mic_client", None))
            self.mic_client = None
            self.win.destroy()
            self.win = None
            self.m.save()

    def paint(self, key):
        icon, text, slider, picker, label, glyph, muted_glyph = self.rows[key]
        muted = self.m.mute[key]
        icon.config(text=muted_glyph if muted else glyph, fg=DIM if muted else "white")
        text.config(text=f"{label}  {'muted' if muted else str(self.m.vol[key]) + '%'}",
                    fg=DIM if muted else "white")
        slider.draw(self.m.vol[key], DIM if muted else ACCENT)
        dev = self.m.device(key)
        picker.config(text=f"{short_name(dev.FriendlyName) if dev else 'Not connected'}  ▾")

    def pick(self, key, e):
        menu = tk.Menu(self.win, tearoff=0)
        current = (self.m.dev or {}).get(key)
        for d in self.m.choices(key):
            menu.add_command(label=("●  " if d.id == current else "     ") + d.FriendlyName,
                             command=lambda i=d.id: self.choose(key, i))
        self.menu_open = True
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()
            self.root.after(300, lambda: setattr(self, "menu_open", False))

    def choose(self, key, dev_id):
        self.m.set_device(key, dev_id)
        if key == "mic":
            self.m.sync_mic()
            if self.win:
                self.restart_mic_monitor()
                self.meter_sources = self.m.channel_meters()
        if self.win:
            self.paint(key)
            self.win.focus_force()

    def paint_dts(self):
        cur = self.dts.preset_key if self.dts.enabled else "off"
        for key, b in self.dts_buttons.items():
            on = key == cur
            b.config(bg=ACCENT if on else "#2d2d2d", fg="black" if on else "white")

    def pick_dts(self, key):
        if key == "off":
            self.dts.enabled = False
        else:
            self.dts.set_preset(key)
        self.paint_dts()

    def paint_nc(self):
        if not self.nc_buttons:
            return
        on = self.nc and self.nc.running
        for key, b in self.nc_buttons.items():
            active = (key == "on") == on
            b.config(bg=ACCENT if active else "#2d2d2d", fg="black" if active else "white")

    def pick_nc(self, key):
        if not self.nc:
            return
        if key == "on" and not self.nc.running:
            self.nc.start()
        elif key == "off" and self.nc.running:
            self.nc.stop()
        self.paint_nc()

    def change(self, key, value):
        self.m.set(key, value=value)
        self.paint(key)

    def toggle_mute(self, key):
        self.m.set(key, mute=not self.m.mute[key])
        self.m.save()
        self.paint(key)
