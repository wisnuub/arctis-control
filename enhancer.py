"""
Experimental mic enhancement: a small broadcast-style voice chain run on each 10 ms
block right after DeepFilterNet, tuned for the Arctis 7 mic (measured 2026-10-31:
usable ~100 Hz to ~6.5 kHz, hard cut above ~7 kHz, so it is 16 kHz audio inside).

  low cut -> EQ (warmth / less box / presence) -> de-esser -> compressor -> exciter -> limiter

Filters are designed as biquads, turned once into a 2048-tap impulse response, then
applied per block by FFT overlap-add with numpy (no SciPy: it would double the app's
size). The dynamics work per block with gain ramps. No added latency.
The exciter adds a little synthetic "air" above 7 kHz from harmonics of the 3-6 kHz
band, since the mic itself records nothing up there.
"""
import math

import numpy as np

SR = 48000
BLOCK_S = 0.01

PRESETS = [("off", "Off"), ("natural", "Natural"), ("broadcast", "Broadcast")]

SETTINGS = {
    "natural": {
        "eq": [("hp", 80, 0.707, 0), ("hp", 80, 0.707, 0)],          # 24 dB/oct rumble cut
        "deess": None,
        "comp": {"thresh": -30, "ratio": 2.0, "makeup": 4},
        "exciter": 0.0,
    },
    "broadcast": {
        "eq": [("hp", 90, 0.707, 0), ("hp", 90, 0.707, 0),
               ("lowshelf", 180, 0.707, 3.0),                        # warmth
               ("peak", 400, 1.0, -3.0),                             # less boxy
               ("peak", 3200, 0.9, 4.0),                             # presence / clarity
               ("highshelf", 5000, 0.707, 2.0)],                     # top of what the mic has
        "deess": {"f0": 5500, "q": 1.4, "thresh": -38, "max_cut": 0.6},
        "comp": {"thresh": -32, "ratio": 3.5, "makeup": 8},
        "exciter": 0.12,
    },
}
LIMIT_DB = -1.0


def biquad(kind, f0, q=0.707, gain_db=0.0):
    """RBJ audio-EQ-cookbook biquad as one SOS row [b0 b1 b2 1 a1 a2]."""
    a_lin = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / SR
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "hp":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bp":                                  # 0 dB peak gain
        b = [alpha, 0.0, -alpha]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * a_lin, -2 * cw, 1 - alpha * a_lin]
        a = [1 + alpha / a_lin, -2 * cw, 1 - alpha / a_lin]
    elif kind in ("lowshelf", "highshelf"):
        s = 2 * math.sqrt(a_lin) * alpha
        if kind == "lowshelf":
            b = [a_lin * ((a_lin + 1) - (a_lin - 1) * cw + s),
                 2 * a_lin * ((a_lin - 1) - (a_lin + 1) * cw),
                 a_lin * ((a_lin + 1) - (a_lin - 1) * cw - s)]
            a = [(a_lin + 1) + (a_lin - 1) * cw + s,
                 -2 * ((a_lin - 1) + (a_lin + 1) * cw),
                 (a_lin + 1) + (a_lin - 1) * cw - s]
        else:
            b = [a_lin * ((a_lin + 1) + (a_lin - 1) * cw + s),
                 -2 * a_lin * ((a_lin - 1) + (a_lin + 1) * cw),
                 a_lin * ((a_lin + 1) + (a_lin - 1) * cw - s)]
            a = [(a_lin + 1) - (a_lin - 1) * cw + s,
                 2 * ((a_lin - 1) - (a_lin + 1) * cw),
                 (a_lin + 1) - (a_lin - 1) * cw - s]
    else:
        raise ValueError(kind)
    return [b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]]


class _Filter:
    """Biquad cascade applied as a long FIR (its impulse response) by FFT overlap-add."""

    TAPS = 2048                                   # ~43 ms: covers the 80 Hz low cut's ringing

    def __init__(self, rows, block=480):
        h = np.zeros(self.TAPS)
        h[0] = 1.0
        for b0, b1, b2, _, a1, a2 in rows:       # once per preset change, not per block
            y, z1, z2 = np.empty(self.TAPS), 0.0, 0.0
            for i, x in enumerate(h):             # transposed direct form II
                out = b0 * x + z1
                z1 = b1 * x - a1 * out + z2
                z2 = b2 * x - a2 * out
                y[i] = out
            h = y
        self.nfft = 1 << (self.TAPS + block - 1).bit_length()
        self.H = np.fft.rfft(h, self.nfft)
        self.tail = np.zeros(self.TAPS - 1)

    def __call__(self, x):
        n = len(x)
        acc = np.fft.irfft(np.fft.rfft(x, self.nfft) * self.H, self.nfft)[:n + self.TAPS - 1]
        acc[:self.TAPS - 1] += self.tail
        self.tail = acc[n:].copy()
        return acc[:n]


def _db(x):
    return 20 * math.log10(max(x, 1e-9))


class Enhancer:
    def __init__(self, preset="off"):
        self.preset = preset if preset in SETTINGS else "off"
        cfg = SETTINGS.get(self.preset)
        if not cfg:
            return
        self.cfg = cfg
        self.eq = _Filter([biquad(*row) for row in cfg["eq"]])
        d = cfg["deess"]
        self.deess_band = _Filter([biquad("bp", d["f0"], d["q"])]) if d else None
        self.deess_cut = 0.0
        self.exc_band = _Filter([biquad("bp", 4500, 0.8)])
        self.exc_hp = _Filter([biquad("hp", 7500, 0.707), biquad("hp", 7500, 0.707)])
        self.comp_gain = 1.0                              # linear, smoothed across blocks
        self.lim_gain = 1.0
        self.makeup = 10 ** (cfg["comp"]["makeup"] / 20)
        self.ceiling = 10 ** (LIMIT_DB / 20)

    def process(self, block):
        """block: float32 mono 10 ms frame -> processed float32 frame (same length)."""
        if self.preset == "off":
            return block
        cfg = self.cfg
        x = self.eq(block.astype(np.float64))
        n = len(x)

        # de-esser: when the 4.5-6.5 kHz band is loud relative to the voice, pull it down
        if self.deess_band is not None:
            band = self.deess_band(x)
            band_db = _db(math.sqrt(float(np.mean(band * band))))
            d = cfg["deess"]
            target = min(d["max_cut"], max(0.0, (band_db - d["thresh"]) / 12.0))
            self.deess_cut += (target - self.deess_cut) * (0.6 if target > self.deess_cut else 0.15)
            x = x - self.deess_cut * band

        # compressor: block RMS -> gain, fast attack / slow release, ramped across the block
        c = cfg["comp"]
        level = _db(math.sqrt(float(np.mean(x * x))))
        over = level - c["thresh"]
        target = 10 ** (-(over * (1 - 1 / c["ratio"])) / 20) if over > 0 else 1.0
        coeff = 0.5 if target < self.comp_gain else 0.08     # ~20 ms attack, ~120 ms release
        new_gain = self.comp_gain + (target - self.comp_gain) * coeff
        x = x * np.linspace(self.comp_gain, new_gain, n) * self.makeup
        self.comp_gain = new_gain

        # exciter: harmonics of the 3-6 kHz band, keep only what lands above ~7.5 kHz
        if cfg["exciter"]:
            sat = np.tanh(self.exc_band(x) * 4.0)
            x = x + cfg["exciter"] * self.exc_hp(sat)

        # limiter: keep peaks under -1 dBFS, then a hard safety clip
        peak = float(np.max(np.abs(x))) or 1e-9
        target = min(1.0, self.ceiling / peak)
        new_lim = target if target < self.lim_gain else self.lim_gain + (1.0 - self.lim_gain) * 0.05
        x = x * np.linspace(self.lim_gain, new_lim, n)
        self.lim_gain = new_lim
        return np.clip(x, -self.ceiling, self.ceiling).astype(np.float32)
