"""
Real-time noise cancellation via DeepFilterNet LADSPA plugin.

Pipeline: Arctis 7 Chat mic -> DeepFilterNet -> VB-CABLE ("CABLE Input" playback),
which comes back out of VB-CABLE's recording side as a clean virtual mic. While it
runs, that virtual mic is made the Windows default recording device (apps left on
"Default" follow); turning it off switches back to the Arctis mic. The on/off state
is remembered between launches. VB-CABLE (free donationware, vb-cable.com) must be
installed by the user: its licence doesn't allow bundling it. Voicemeeter works too.
The mixer shows raw vs processed levels on the Mic meter bar.
"""
import ctypes
import json
import logging
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

from enhancer import Enhancer
from paths import DATA_DIR, RES_DIR

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME_SIZE = 480  # 10 ms at 48 kHz
DLL_NAME = "deep_filter_ladspa.dll"
STATE_FILE = DATA_DIR / "noise_cancel.json"
CABLE_DRIVER = "VB-Audio Virtual Cable"      # stays in the name even if the endpoint is renamed
SOURCE_MIC = "Arctis 7 Chat"

VIRTUAL_OUTPUT_NAMES = [
    "CABLE Input",
    "VB-Cable",
    "Voicemeeter Point 1",
    "Voicemeeter Point 2",
    "Voicemeeter VAIO",
]


class _LADSPA_PortRangeHint(ctypes.Structure):
    _fields_ = [
        ("HintDescriptor", ctypes.c_int),
        ("LowerBound", ctypes.c_float),
        ("UpperBound", ctypes.c_float),
    ]


class _LADSPA_Descriptor(ctypes.Structure):
    _fields_ = [
        ("UniqueID", ctypes.c_ulong),
        ("Label", ctypes.c_char_p),
        ("Properties", ctypes.c_int),
        ("Name", ctypes.c_char_p),
        ("Maker", ctypes.c_char_p),
        ("Copyright", ctypes.c_char_p),
        ("PortCount", ctypes.c_ulong),
        ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
        ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
        ("PortRangeHints", ctypes.POINTER(_LADSPA_PortRangeHint)),
        ("ImplementationData", ctypes.c_void_p),
        ("instantiate", ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)),
        ("connect_port", ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_float))),
        ("activate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("run", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)),
        ("run_adding", ctypes.c_void_p),
        ("set_run_adding_gain", ctypes.c_void_p),
        ("deactivate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("cleanup", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
    ]


def _find_dll() -> Path:
    for p in (RES_DIR / DLL_NAME, Path(__file__).parent / DLL_NAME):
        if p.exists():
            return p
    raise FileNotFoundError(
        f"{DLL_NAME} not found in {RES_DIR} or {Path(__file__).parent}")


def _find_device(name_fragment: str, kind: str,
                 hostapi: int | None = None) -> int | None:
    for i, d in enumerate(sd.query_devices()):
        if hostapi is not None and d["hostapi"] != hostapi:
            continue
        if name_fragment.lower() in d["name"].lower():
            if kind == "input" and d["max_input_channels"] > 0:
                return i
            if kind == "output" and d["max_output_channels"] > 0:
                return i
    return None


def _wasapi() -> int | None:
    return next((i for i, h in enumerate(sd.query_hostapis()) if "WASAPI" in h["name"]), None)


def _find_virtual_output() -> tuple[int | None, str]:
    for hostapi in (_wasapi(), None):
        for name in VIRTUAL_OUTPUT_NAMES:
            idx = _find_device(name, "output", hostapi)
            if idx is not None:
                return idx, sd.query_devices(idx)["name"]
    return None, ""


def _find_paired_input(output_name: str) -> str:
    """Given a virtual cable output name, find the paired input device name.

    Voicemeeter Point N output -> 'Voicemeeter Out N (Voicemeeter Point N)' input
    VB-CABLE -> 'CABLE Output (VB-Audio Virtual Cable)' input
    """
    if "Voicemeeter Point" in output_name:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and "Voicemeeter Out" in d["name"]:
                for frag in ("Point 1", "Point 2", "Point 3"):
                    if frag in output_name and frag in d["name"]:
                        return d["name"]
    elif "CABLE" in output_name or "VB-Cable" in output_name:
        wasapi = _wasapi()
        for d in sd.query_devices():
            if (d["max_input_channels"] > 0 and CABLE_DRIVER in d["name"]
                    and d["hostapi"] == wasapi):
                return d["name"]
    return ""


def _set_default_mic(to_virtual: bool):
    """Make the clean virtual mic (or the Arctis mic again) the Windows default for
    every role, so apps left on "Default" follow noise cancelling on/off."""
    from pycaw.constants import ERole
    from pycaw.pycaw import AudioUtilities
    mics = AudioUtilities.GetAllDevices(data_flow=1, device_state=1)
    want = CABLE_DRIVER if to_virtual else SOURCE_MIC
    dev = next((d for d in mics if want in (d.FriendlyName or "")), None)
    if dev:
        AudioUtilities.SetDefaultDevice(
            dev.id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
        log.info("NC: default mic -> %s", dev.FriendlyName)


def was_enabled() -> bool:
    try:
        # utf-8-sig: tolerate a BOM if the file was edited by hand (PowerShell 5 adds one)
        return bool(json.loads(STATE_FILE.read_text(encoding="utf-8-sig")).get("enabled"))
    except (FileNotFoundError, ValueError):
        return False


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, ValueError):
        return {}


def _remember(enabled: bool = None, enhance: str = None):
    """Merge into noise_cancel.json: {"enabled": bool, "enhance": preset}."""
    state = _load_state()
    if enabled is not None:
        state["enabled"] = enabled
    if enhance is not None:
        state["enhance"] = enhance
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _wasapi_settings(in_dev, out_dev):
    """Let WASAPI resample if a device isn't at 48 kHz (WASAPI devices only)."""
    wasapi = _wasapi()
    pick = lambda d: (sd.WasapiSettings(auto_convert=True)
                      if wasapi is not None and sd.query_devices(d)["hostapi"] == wasapi else None)
    return (pick(in_dev), pick(out_dev))


class NoiseCanceller:
    """Real-time noise cancellation engine.

    Captures Arctis mic -> LADSPA DeepFilterNet -> virtual audio output.
    Exposes raw_peak / processed_peak for the mixer level meter.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._available = False
        self._running = False
        self._paused = False
        self._stream = None
        self._instance = None
        self._desc = None
        self._desc_ptr = None
        self._dll = None
        self._err = None

        self._in_buf = np.zeros(FRAME_SIZE, dtype=np.float32)
        self._out_buf = np.zeros(FRAME_SIZE, dtype=np.float32)

        self._atten_limit = ctypes.c_float(100.0)
        self._min_thresh = ctypes.c_float(-10.0)
        self._max_erb = ctypes.c_float(30.0)
        self._max_df = ctypes.c_float(20.0)
        self._min_proc_buf = ctypes.c_float(0.0)
        self._post_beta = ctypes.c_float(0.02)

        self._input_dev = None
        self._output_dev = None
        self._input_dev_name = ""
        self._output_dev_name = ""
        self._paired_input_name = ""

        self._raw_peak = 0.0
        self._proc_peak = 0.0
        self.enhancer = Enhancer(_load_state().get("enhance", "off"))   # experimental voice chain

        self._init()

    def _init(self):
        try:
            dll_path = _find_dll()
            self._dll = ctypes.CDLL(str(dll_path))
            get_desc = self._dll.ladspa_descriptor
            get_desc.restype = ctypes.POINTER(_LADSPA_Descriptor)
            get_desc.argtypes = [ctypes.c_ulong]
            self._desc_ptr = get_desc(0)
            if not self._desc_ptr:
                self._err = "no LADSPA descriptor"
                return
            self._desc = self._desc_ptr.contents
            log.info("NC: loaded %s (%s)", self._desc.Name.decode(),
                     dll_path.name)
        except Exception as e:
            self._err = f"DLL load failed: {e}"
            log.warning("NC: %s", self._err)
            return

        self._output_dev, self._output_dev_name = _find_virtual_output()

        if self._output_dev is not None:
            out_api = sd.query_devices(self._output_dev)["hostapi"]
            self._input_dev = _find_device(SOURCE_MIC, "input", out_api)
        if self._input_dev is None:
            self._input_dev = _find_device(SOURCE_MIC, "input")
        if self._input_dev is not None:
            self._input_dev_name = sd.query_devices(self._input_dev)["name"]
        if self._output_dev_name:
            self._paired_input_name = _find_paired_input(self._output_dev_name)

        if self._input_dev is None:
            self._err = "Arctis 7 Chat mic not found"
            log.warning("NC: %s", self._err)
            return
        if self._output_dev is None:
            self._err = "No virtual audio cable found (install VB-CABLE)"
            log.warning("NC: %s", self._err)
            return

        self._available = True
        log.info("NC: in=%s out=%s", self._input_dev_name,
                 self._output_dev_name)

    @property
    def available(self):
        return self._available

    @property
    def running(self):
        return self._running

    @property
    def error(self):
        return self._err

    @property
    def output_device_name(self):
        return self._output_dev_name

    @property
    def input_device_hint(self):
        return self._paired_input_name

    @property
    def raw_peak(self) -> float:
        return self._raw_peak

    @property
    def processed_peak(self) -> float:
        return self._proc_peak

    def _create_instance(self):
        inst = self._desc.instantiate(self._desc_ptr, SAMPLE_RATE)
        if not inst:
            raise RuntimeError("LADSPA instantiate returned NULL")

        d = self._desc
        in_ptr = self._in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        out_ptr = self._out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        d.connect_port(inst, 0, in_ptr)
        d.connect_port(inst, 1, out_ptr)
        d.connect_port(inst, 2, ctypes.pointer(self._atten_limit))
        d.connect_port(inst, 3, ctypes.pointer(self._min_thresh))
        d.connect_port(inst, 4, ctypes.pointer(self._max_erb))
        d.connect_port(inst, 5, ctypes.pointer(self._max_df))
        d.connect_port(inst, 6, ctypes.pointer(self._min_proc_buf))
        d.connect_port(inst, 7, ctypes.pointer(self._post_beta))
        d.activate(inst)

        for _ in range(5):
            d.run(inst, FRAME_SIZE)
        self._out_buf[:] = 0.0

        return inst

    def _destroy_instance(self):
        if self._instance and self._desc:
            try:
                self._desc.deactivate(self._instance)
                self._desc.cleanup(self._instance)
            except Exception:
                pass
            self._instance = None

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.debug("NC stream status: %s", status)
        n = min(frames, FRAME_SIZE)
        self._in_buf[:n] = indata[:n, 0]
        raw = float(np.max(np.abs(self._in_buf[:n])))
        self._desc.run(self._instance, n)
        out = self._out_buf[:n]
        enhancer = self.enhancer                  # read once: the UI may swap it
        if enhancer.preset != "off":
            try:
                out = enhancer.process(out)
            except Exception as e:                # never let the voice chain kill the mic
                log.warning("NC: enhancer failed, disabling: %s", e)
                self.enhancer = Enhancer("off")
        proc = float(np.max(np.abs(out)))
        self._raw_peak = raw
        self._proc_peak = proc
        outdata[:n, 0] = out

    def start(self):
        with self._lock:
            if not self._available or self._running:
                return False
            try:
                self._instance = self._create_instance()
                self._stream = sd.Stream(
                    samplerate=SAMPLE_RATE,
                    blocksize=FRAME_SIZE,
                    device=(self._input_dev, self._output_dev),
                    channels=1,
                    dtype="float32",
                    callback=self._audio_callback,
                    latency="low",
                    extra_settings=_wasapi_settings(self._input_dev, self._output_dev),
                )
                self._stream.start()
                self._running = True
                _remember(True)
                if CABLE_DRIVER in self._output_dev_name or "CABLE" in self._output_dev_name:
                    try:
                        _set_default_mic(True)
                    except Exception as e:
                        log.warning("NC: default mic switch failed: %s", e)
                log.info("NC: started (%s -> %s)",
                         self._input_dev_name, self._output_dev_name)
                return True
            except Exception as e:
                log.error("NC start failed: %s", e)
                self._destroy_instance()
                self._err = str(e)
                return False

    def stop(self, remember=True):
        """remember=False on app exit, so it comes back on at next launch."""
        with self._lock:
            if not self._running:
                return
            try:
                if self._stream:
                    self._stream.stop()
                    self._stream.close()
                    self._stream = None
            except Exception:
                pass
            self._destroy_instance()
            self._running = False
            self._paused = False
            self._raw_peak = 0.0
            self._proc_peak = 0.0
            if remember:
                _remember(False)
            try:
                _set_default_mic(False)
            except Exception as e:
                log.warning("NC: default mic switch failed: %s", e)
            log.info("NC: stopped")

    @property
    def enhance_preset(self):
        return self.enhancer.preset

    def set_enhance(self, preset: str):
        """Swap the voice chain (fresh filter state); applies while noise cancel runs."""
        self.enhancer = Enhancer(preset)
        _remember(enhance=self.enhancer.preset)
        log.info("NC: mic enhancement -> %s", self.enhancer.preset)

    def pause(self):
        """Headset off: stop the DSP (it costs ~30% of a core even on silence) but keep
        the virtual mic as default and the 'on' setting, so resume() just restarts it."""
        with self._lock:
            if not self._running or self._paused:
                return
            try:
                self._stream.stop()
            except Exception:
                pass
            self._paused = True
            self._raw_peak = self._proc_peak = 0.0
            log.info("NC: paused (headset off)")

    def resume(self):
        with self._lock:
            if not self._running or not self._paused:
                return
            try:
                self._stream.start()
                self._paused = False
                log.info("NC: resumed")
            except Exception as e:
                log.warning("NC: resume failed: %s", e)

    def toggle(self):
        if self._running:
            self.stop()
        else:
            self.start()
        return self._running
