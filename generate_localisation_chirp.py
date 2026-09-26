"""Generate the WAV used for binaural localisation tests."""

import argparse
import math
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48000
START_HZ = 400.0
END_HZ = 8000.0
DURATION_S = 0.080
LEVEL = 0.65
FADE_S = 0.005

def make_chirp(sample_rate, start_hz, end_hz, duration_s, level):
    n = int(round(sample_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / sample_rate

    # Logarithmic chirp: equal time per octave gives useful energy across both
    # the low-frequency ITD band and higher-frequency ILD band.
    ratio = end_hz / start_hz
    phase = (
        2.0 * np.pi * start_hz * duration_s / math.log(ratio)
        * (np.power(ratio, t / duration_s) - 1.0)
    )
    x = np.sin(phase)

    fade_n = max(1, int(round(sample_rate * FADE_S)))
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade_n))
    x[:fade_n] *= ramp
    x[-fade_n:] *= ramp[::-1]
    return level * x

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="localisation_chirp.wav")
    args = parser.parse_args()

    x = make_chirp(SAMPLE_RATE, START_HZ, END_HZ, DURATION_S, LEVEL)
    pcm = np.clip(x * 32767.0, -32768, 32767).astype("<i2")

    path = Path(args.output)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())

    print(f"Wrote {path}")
    print(f"{START_HZ:.0f}-{END_HZ:.0f} Hz logarithmic chirp, "
          f"{DURATION_S*1000:.0f} ms, {SAMPLE_RATE} Hz")

if __name__ == "__main__":
    main()
