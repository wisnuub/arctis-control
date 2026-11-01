"""
DeepFilterNet3 noise suppression on ONNX Runtime, on the CPU or the GPU (DirectML).

models/dfn3_stream.onnx is the official DFN3 model merged into one per-frame graph
(tools/make_dfn_stream.py). The DSP around it follows libDF: Vorbis-window STFT
(960/480 at 48 kHz), ERB and complex features with exponential normalisation, then ERB
gains plus deep filtering applied two frames back (the model's lookahead) and
overlap-add. Every frame runs the full model, like the official PyTorch enhance(),
whose output this matches to int16 precision; libDF's runtime instead skips stages by
an SNR estimate to save CPU, which costs quality and isn't needed at ~0.5 ms a frame.
Latency: one 10 ms hop plus the 20 ms lookahead.
"""
from collections import deque
import math

import numpy as np
import onnxruntime as ort

SR, FFT, HOP = 48000, 960, 480
NB_ERB, NB_DF, DF_ORDER, LOOKAHEAD = 32, 96, 5, 2
ALPHA = 0.99                         # norm_tau 1 s at 100 frames/s, rounded like libDF


def _erb_widths(nb_bands=NB_ERB, min_nb_freqs=2):
    """Bins per ERB band, as libDF's erb_fb()."""
    freq2erb = lambda f: 9.265 * math.log1p(f / (24.7 * 9.265))
    erb2freq = lambda e: 24.7 * 9.265 * (math.exp(e / 9.265) - 1)
    step = freq2erb(SR / 2) / nb_bands
    widths, prev, over = [], 0, 0
    for i in range(1, nb_bands + 1):
        fb = round(erb2freq(i * step) / (SR / FFT))
        n = fb - prev - over
        over, n = (min_nb_freqs - n, min_nb_freqs) if n < min_nb_freqs else (0, n)
        widths.append(n)
        prev = fb
    widths[-1] += 1                                   # rfft has FFT/2+1 bins
    widths[-1] -= sum(widths) - (FFT // 2 + 1)
    return np.array(widths)


def available_devices():
    return ["CPU", "GPU"] if "DmlExecutionProvider" in ort.get_available_providers() else ["CPU"]


class DeepFilter:
    def __init__(self, model_path, gpu=False):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        if gpu:
            so.enable_mem_pattern = False                # DirectML requirements
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        else:
            so.intra_op_num_threads = so.inter_op_num_threads = 1   # tiny model: threads only add overhead
            providers = ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(model_path), so, providers=providers)
        self.device = "GPU" if self.session.get_providers()[0] == "DmlExecutionProvider" else "CPU"
        self.state_names = [i.name for i in self.session.get_inputs()
                            if not i.name.startswith("enc/feat")]
        self.state_shapes = {i.name: i.shape for i in self.session.get_inputs()}

        i = np.arange(FFT)
        self.window = np.sin(0.5 * np.pi * np.sin(0.5 * np.pi * (i + 0.5) / HOP) ** 2)
        self.wnorm = 1.0 / (FFT ** 2 / (2 * HOP))
        self.widths = _erb_widths()
        self.band_starts = np.concatenate(([0], np.cumsum(self.widths)[:-1]))
        self.reset()

    def reset(self):
        self.ana_mem = np.zeros(HOP)
        self.syn_mem = np.zeros(HOP)
        self.mean_norm = np.linspace(-60.0, -90.0, NB_ERB)
        self.unit_norm = np.linspace(0.001, 0.0001, NB_DF)
        self.feat_erb = np.zeros((1, 1, 3, NB_ERB), np.float32)
        self.feat_spec = np.zeros((1, 2, 3, NB_DF), np.float32)
        zeros = lambda: np.zeros(FFT // 2 + 1, np.complex128)
        self.noisy = deque((zeros() for _ in range(DF_ORDER)), maxlen=DF_ORDER)
        self.state = {n: np.zeros(self.state_shapes[n], np.float32) for n in self.state_names}
        self.silent = 0

    def process(self, x):
        """One 480-sample float32 frame in, one out (delayed by the model's lookahead)."""
        if float(np.dot(x, x)) / HOP < 1e-7:            # digital silence: skip like libDF
            self.silent += 1
            if self.silent > 5:
                return np.zeros(HOP, np.float32)
        else:
            self.silent = 0

        spec = np.fft.rfft(np.concatenate((self.ana_mem, x)) * self.window) * self.wnorm
        self.ana_mem = np.array(x, np.float64)
        self.noisy.append(spec)

        power = np.add.reduceat(spec.real ** 2 + spec.imag ** 2, self.band_starts) / self.widths
        erb = 10 * np.log10(power + 1e-10)
        self.mean_norm = erb * (1 - ALPHA) + self.mean_norm * ALPHA
        cplx = spec[:NB_DF]
        self.unit_norm = np.abs(cplx) * (1 - ALPHA) + self.unit_norm * ALPHA
        cplx = cplx / np.sqrt(self.unit_norm)
        self.feat_erb[..., :2, :] = self.feat_erb[..., 1:, :]
        self.feat_erb[0, 0, 2] = (erb - self.mean_norm) / 40
        self.feat_spec[..., :2, :] = self.feat_spec[..., 1:, :]
        self.feat_spec[0, 0, 2], self.feat_spec[0, 1, 2] = cplx.real, cplx.imag

        out = self.session.run(None, {"enc/feat_erb": self.feat_erb,
                                      "enc/feat_spec": self.feat_spec, **self.state})
        self.state = dict(zip(self.state_names, out[3:]))

        enh = self.noisy[DF_ORDER - 1 - LOOKAHEAD].copy()   # the frame LOOKAHEAD hops back
        enh *= np.repeat(out[1].ravel(), self.widths)    # ERB gains
        coefs = out[2].reshape(NB_DF, DF_ORDER, 2)      # deep filter over 5 noisy frames
        window = np.stack([s[:NB_DF] for s in self.noisy], axis=1)
        enh[:NB_DF] = (window * (coefs[..., 0] + 1j * coefs[..., 1])).sum(axis=1)

        y = np.fft.irfft(enh, FFT) * FFT * self.window
        frame = y[:HOP] + self.syn_mem
        self.syn_mem = y[HOP:]
        return frame.astype(np.float32)
