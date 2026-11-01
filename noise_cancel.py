"""
Real-time noise cancellation with DeepFilterNet3 (deepfilter.py, ONNX Runtime on the
CPU or, if picked, the GPU through DirectML).

Pipeline: Arctis 7 Chat mic -> DeepFilterNet -> VB-CABLE ("CABLE Input" playback),
which comes back out of VB-CABLE's recording side as a clean virtual mic. While it
runs, that virtual mic is made the Windows default recording device (apps left on
"Default" follow); turning it off switches back to the Arctis mic. The on/off state
is remembered between launches. VB-CABLE (free donationware, vb-cable.com) must be
installed by the user: its licence doesn't allow bundling it. Voicemeeter works too.
The mixer shows raw vs processed levels on the Mic meter bar.
"""
import json
import logging
import threading

import numpy as np
import sounddevice as sd

from deepfilter import DeepFilter, available_devices
from enhancer import Enhancer
from paths import DATA_DIR, RES_DIR

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME_SIZE = 480  # 10 ms at 48 kHz
MODEL = RES_DIR / "models" / "dfn3_stream.onnx"
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


def _remember(enabled: bool = None, enhance: str = None, gpu: bool = None):
    """Merge into noise_cancel.json: {"enabled": bool, "enhance": preset, "gpu": bool}."""
    state = _load_state()
    if enabled is not None:
        state["enabled"] = enabled
    if enhance is not None:
        state["enhance"] = enhance
    if gpu is not None:
        state["gpu"] = gpu
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _wasapi_settings(in_dev, out_dev):
    """Let WASAPI resample if a device isn't at 48 kHz (WASAPI devices only)."""
    wasapi = _wasapi()
    pick = lambda d: (sd.WasapiSettings(auto_convert=True)
                      if wasapi is not None and sd.query_devices(d)["hostapi"] == wasapi else None)
    return (pick(in_dev), pick(out_dev))


class NoiseCanceller:
    """Real-time noise cancellation engine.

    Captures Arctis mic -> DeepFilterNet -> virtual audio output.
    Exposes raw_peak / processed_peak for the mixer level meter.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._available = False
        self._running = False
        self._paused = False
        self._stream = None
        self._engine = None
        self._err = None
        state = _load_state()
        self._gpu = bool(state.get("gpu")) and "GPU" in available_devices()

        self._input_dev = None
        self._output_dev = None
        self._input_dev_name = ""
        self._output_dev_name = ""
        self._paired_input_name = ""

        self._raw_peak = 0.0
        self._proc_peak = 0.0
        self.enhancer = Enhancer(state.get("enhance", "off"))   # experimental voice chain

        self._init()

    def _init(self):
        if not MODEL.exists():
            self._err = f"model missing: {MODEL}"
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
    def device(self):
        """"CPU" or "GPU": what inference runs on (GPU falls back to CPU if it fails)."""
        engine = self._engine
        return engine.device if engine else ("GPU" if self._gpu else "CPU")

    @property
    def gpu_available(self):
        return "GPU" in available_devices()

    def set_gpu(self, on: bool):
        """Pick CPU or GPU inference; swaps the engine live if noise cancel is running."""
        self._gpu = on
        _remember(gpu=on)
        if self._running:
            self._engine = self._make_engine()   # the audio thread picks it up next frame
        log.info("NC: inference on %s", self.device)

    @property
    def raw_peak(self) -> float:
        return self._raw_peak

    @property
    def processed_peak(self) -> float:
        return self._proc_peak

    def _make_engine(self):
        if self._gpu:
            try:
                return DeepFilter(MODEL, gpu=True)
            except Exception as e:
                log.warning("NC: GPU unavailable, using CPU: %s", e)
        return DeepFilter(MODEL)

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        if status:
            log.debug("NC stream status: %s", status)
        x = indata[:, 0]
        engine = self._engine                     # read once: set_gpu may swap it
        try:
            out = engine.process(x)
        except Exception as e:
            if engine.device != "GPU":
                raise
            # e.g. the GPU driver reset after a game crash: carry on on the CPU
            log.warning("NC: GPU inference failed, switching to CPU: %s", e)
            self._engine = engine = DeepFilter(MODEL)
            out = engine.process(x)
        raw = float(np.max(np.abs(x)))
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
        outdata[:, 0] = out

    def start(self):
        with self._lock:
            if not self._available or self._running:
                return False
            try:
                self._engine = self._make_engine()
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
                log.info("NC: started on %s (%s -> %s)", self._engine.device,
                         self._input_dev_name, self._output_dev_name)
                return True
            except Exception as e:
                log.error("NC start failed: %s", e)
                if self._stream:
                    self._stream.close()
                    self._stream = None
                self._engine = None
                self._running = False
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
            self._engine = None
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
        """Headset off: stop processing but keep the virtual mic as default and the 'on'
        setting, so resume() just restarts it."""
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
