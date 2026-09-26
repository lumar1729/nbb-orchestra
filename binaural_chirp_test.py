"""Single-Pi binaural test with chirp automatically played by the server."""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request

import matplotlib.pyplot as plt
import numpy as np

import LBB.config as Config
import NB3.Sound.microphone as Microphone
import NB3.Sound.utilities as Utilities

SAMPLE_RATE = 48000
EAR_DISTANCE_M = 0.17
SPEED_OF_SOUND = 343.0
ITD_LOW_HZ = 400.0
ITD_HIGH_HZ = 1800.0
ILD_LOW_HZ = 2000.0
ILD_HIGH_HZ = 8000.0

def fft_bandpass(x, low_hz, high_hz):
    x = np.asarray(x, dtype=np.float64)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spectrum[(freqs < low_hz) | (freqs > high_hz)] = 0
    return np.fft.irfft(spectrum, n=len(x))

def correlations_for_lags(left, right, max_lag):
    left = left - np.mean(left)
    right = right - np.mean(right)
    lags = np.arange(-max_lag, max_lag + 1)
    corr = np.zeros(len(lags))
    for k, lag in enumerate(lags):
        if lag > 0:
            a, b = left[:-lag], right[lag:]
        elif lag < 0:
            a, b = left[-lag:], right[:lag]
        else:
            a, b = left, right
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        corr[k] = np.dot(a, b) / denom if denom else 0.0
    return lags, corr

def refine_peak(y, i):
    if i == 0 or i == len(y) - 1:
        return float(i)
    a, b, c = y[i-1], y[i], y[i+1]
    denom = a - 2*b + c
    if abs(denom) < 1e-12:
        return float(i)
    return i + float(np.clip(0.5 * (a-c) / denom, -1, 1))

def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))

def request_server_chirp(server, port):
    url = f"http://{server}:{port}/play_chirp"
    body = urllib.parse.urlencode({"source": "binaural_test"}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        reply = json.loads(response.read().decode("utf-8"))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "server did not play chirp"))

def analyse(recording, ear_distance):
    left = recording[:,0].astype(np.float64)
    right = recording[:,1].astype(np.float64)
    left -= np.mean(left); right -= np.mean(right)

    li = fft_bandpass(left, ITD_LOW_HZ, ITD_HIGH_HZ)
    ri = fft_bandpass(right, ITD_LOW_HZ, ITD_HIGH_HZ)
    max_lag = int(np.ceil(ear_distance / SPEED_OF_SOUND * SAMPLE_RATE))
    lags, corr = correlations_for_lags(li, ri, max_lag)
    peak = int(np.argmax(corr))
    lag_samples = lags[0] + refine_peak(corr, peak)
    itd = lag_samples / SAMPLE_RATE
    bearing = float(np.degrees(np.arcsin(np.clip(
        SPEED_OF_SOUND * itd / ear_distance, -1.0, 1.0))))

    lh = fft_bandpass(left, ILD_LOW_HZ, ILD_HIGH_HZ)
    rh = fft_bandpass(right, ILD_LOW_HZ, ILD_HIGH_HZ)
    ild = 20*np.log10((rms(lh)+1e-12)/(rms(rh)+1e-12))
    return left, right, lags, corr, lag_samples, itd, bearing, ild, float(corr[peak])

def save_plot(left, right, lags, corr, itd, path):
    t = np.arange(len(left))/SAMPLE_RATE
    scale = max(np.max(np.abs(left)), np.max(np.abs(right)), 1.0)
    fig, ax = plt.subplots(2,1,figsize=(10,7))
    ax[0].plot(t,left/scale,label="Left ear",alpha=.8)
    ax[0].plot(t,right/scale,label="Right ear",alpha=.8)
    ax[0].set(title="Stereo recording",xlabel="Time (s)",ylabel="Normalized amplitude")
    ax[0].legend(); ax[0].grid(True,alpha=.3)
    lag_us = lags/SAMPLE_RATE*1e6
    ax[1].plot(lag_us,corr)
    ax[1].axvline(itd*1e6,linestyle="--",label=f"ITD = {itd*1e6:+.1f} us")
    ax[1].set(title=f"ITD correlation ({ITD_LOW_HZ:.0f}-{ITD_HIGH_HZ:.0f} Hz)",
              xlabel="Right-ear delay relative to left (us)",ylabel="Correlation")
    ax[1].legend(); ax[1].grid(True,alpha=.3)
    fig.tight_layout(); fig.savefig(path,dpi=150); plt.close(fig)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("server", help="Message-board server IP/hostname")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--duration", type=float, default=2.0)
    p.add_argument("--settle", type=float, default=0.5,
                   help="Seconds to record before asking server to chirp")
    p.add_argument("--ear-distance", type=float, default=EAR_DISTANCE_M)
    args = p.parse_args()

    save_path = os.path.join(
        f"{Config.repo_path}/boxes/audio/signal-processing/python/measurement",
        "binaural_chirp_test.png")

    input_device = Utilities.get_input_device_by_name("MAX")
    if input_device == -1:
        raise SystemExit('Input device "MAX" not found')

    buffer_size = SAMPLE_RATE//10
    max_samples = int(SAMPLE_RATE*(args.duration+1))
    mic = Microphone.Microphone(input_device,2,"int32",SAMPLE_RATE,
                                buffer_size,max_samples)
    mic.gain = 10.0

    print(f"Recording stereo audio for {args.duration:.1f} s...")
    mic.start()
    try:
        time.sleep(args.settle)
        print("Requesting chirp from server...")
        request_server_chirp(args.server,args.port)
        remaining = args.duration-args.settle
        if remaining > 0:
            time.sleep(remaining)
        recording = np.copy(mic.sound)
    finally:
        mic.stop()

    recording = recording[-int(SAMPLE_RATE*args.duration):]
    if recording.ndim != 2 or recording.shape[1] < 2:
        raise SystemExit(f"Expected stereo audio; got {recording.shape}")

    left,right,lags,corr,lag,itd,bearing,ild,peak = analyse(
        recording,args.ear_distance)

    print("\nResults")
    print("-------")
    print(f"ITD:              {itd*1e6:+.1f} us")
    print(f"Sample lag:       {lag:+.2f} samples")
    print(f"ILD (L/R):        {ild:+.2f} dB")
    print(f"ITD bearing:      {bearing:+.1f} degrees")
    print(f"Correlation peak: {peak:.3f}")
    print("\n+ bearing = LEFT ear first; - bearing = RIGHT ear first.")
    print("ITD alone has a front/back ambiguity.")
    save_plot(left,right,lags,corr,itd,save_path)
    print(f"\nPlot saved to:\n  {save_path}")

if __name__ == "__main__":
    main()
