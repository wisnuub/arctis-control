"""
DTS Headphone:X v2 controller for SteelSeries Arctis headsets.

Talks directly to the DTS APO driver installed at system level via COM,
replacing SteelSeries GG's SSHZExecutor.  Requires the DTS APO driver
(dtsapo4xhpxv2x64.inf) to be installed (it persists after GG removal).

Mode switching works by writing the serialized mode blob to
HKLM\SOFTWARE\DTS\APO\{endpoint-guid}\DtsApoCurrOpMode.  The APO
picks up changes on the next OpMode read.

Presets (defined in the OEM config XML):
  Off    - bypass, no processing
  Game   - I3DA Game room, no bass, limiter on
  Music  - I3DA Entertainment room, bass +30, limiter +15
  Movie  - I3DA Entertainment room, dialog enhancement, bass +20
"""
import logging
import struct
import threading
import winreg

APO_PATH = r"C:\Windows\System32\DTS\HP\APO4x\SteelSeries"
DTS_SYSTEM_CLSID = "{7766EC50-C2D3-466F-BC7A-9C339C5908A2}"
SPEAKER_TYPE = "APO4-Headphone"

MODE_OFF = "APO4-Off"
MODE_GAME = "APO4-Game1"
MODE_MUSIC = "APO4-Music"
MODE_MOVIE = "APO4-Movie"

PRESETS = [
    ("game",  MODE_GAME,  "Game"),
    ("music", MODE_MUSIC, "Music"),
    ("movie", MODE_MOVIE, "Movie"),
]

log = logging.getLogger(__name__)


def _encode_entry(name: str, value: str) -> bytes:
    nb = name.encode("utf-16-le")
    vb = value.encode("ascii")
    return struct.pack("<III", len(nb), 1, len(vb)) + nb + b"\x06" + vb


def _build_mode_blob(mode_name: str) -> bytes:
    return (_encode_entry("APO4-Operating Mode", mode_name)
            + _encode_entry("APO4-Speaker Type", SPEAKER_TYPE))


def _decode_mode_blob(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    name_len = struct.unpack_from("<I", data, 0)[0]
    val_len = struct.unpack_from("<I", data, 8)[0]
    val_offset = 12 + name_len + 1
    if val_offset + val_len > len(data):
        return None
    return data[val_offset:val_offset + val_len].decode("ascii", errors="replace")


class DTSController:
    """Thin wrapper around the DTS APO COM interface (dtscnt64.dll).

    Public methods are thread-safe (guarded by a lock).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ep = None
        self._mode = None
        self._available = False
        self._enabled = False
        self._current_mode_name = MODE_OFF
        self._reg_subkey = None
        self._err = None
        self._init()

    def _init(self):
        try:
            import comtypes
            import comtypes.client
            tl = comtypes.client.GetModule(APO_PATH + r"\dtscnt64.dll")
            sys_obj = comtypes.CoCreateInstance(
                comtypes.GUID(DTS_SYSTEM_CLSID), interface=tl.IDTSAPOSystem)
            sys_obj.InitializeSystem(APO_PATH)
            ep = sys_obj.EnumerateDTSEndpoints().Next()
            if ep is None:
                self._err = "no DTS endpoint"
                return
            self._ep = ep
            self._mode = ep.OpMode
            self._enabled = bool(self._mode.APOEnable)
            self._reg_subkey = ep.RegistryPath
            self._read_current_mode()
            self._available = True
            log.info("DTS: %s, mode=%s, enabled=%s",
                     ep.FriendlyName, self._current_mode_name, self._enabled)
        except Exception as e:
            self._err = str(e)
            log.warning("DTS init failed: %s", e)

    def _read_current_mode(self):
        if not self._reg_subkey:
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self._reg_subkey)
            data, _ = winreg.QueryValueEx(key, "DtsApoCurrOpMode")
            winreg.CloseKey(key)
            name = _decode_mode_blob(data)
            if name:
                self._current_mode_name = name
        except OSError:
            pass

    def _write_mode(self, mode_name: str):
        if not self._reg_subkey:
            return False
        try:
            blob = _build_mode_blob(mode_name)
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self._reg_subkey,
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DtsApoCurrOpMode", 0, winreg.REG_BINARY, blob)
            winreg.CloseKey(key)
            self._current_mode_name = mode_name
            log.info("DTS mode -> %s", mode_name)
            return True
        except OSError as e:
            log.warning("DTS mode write failed: %s", e)
            return False

    @property
    def available(self):
        return self._available

    @property
    def enabled(self):
        if not self._available:
            return False
        with self._lock:
            try:
                self._enabled = bool(self._mode.APOEnable)
            except Exception:
                pass
            return self._enabled

    @enabled.setter
    def enabled(self, value):
        if not self._available:
            return
        with self._lock:
            try:
                if value:
                    if self._current_mode_name == MODE_OFF:
                        self._write_mode(MODE_GAME)
                    self._mode.APOEnable = 1
                else:
                    self._mode.APOEnable = 0
                    self._write_mode(MODE_OFF)
                self._enabled = bool(value)
                log.info("DTS enabled=%s", self._enabled)
            except Exception as e:
                log.warning("DTS toggle failed: %s", e)

    def toggle(self):
        self.enabled = not self.enabled
        return self.enabled

    def set_preset(self, preset_key: str):
        with self._lock:
            mode_id = next((m for k, m, _ in PRESETS if k == preset_key), None)
            if not mode_id:
                return
            self._write_mode(mode_id)
            self._mode.APOEnable = 1
            self._enabled = True
            log.info("DTS preset -> %s", preset_key)

    @property
    def mode_name(self):
        with self._lock:
            self._read_current_mode()
            return self._current_mode_name

    @property
    def preset_key(self):
        name = self._current_mode_name
        return next((k for k, m, _ in PRESETS if m == name), "off")

    @property
    def preset_label(self):
        name = self._current_mode_name
        return next((lbl for _, m, lbl in PRESETS if m == name), "Off")

    @property
    def error(self):
        return self._err
