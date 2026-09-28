"""Single-Pi binaural test with matched chirp detection and server playback."""

import argparse
import json
import math
import os
import time
from contextlib import contextmanager
import urllib.parse
import urllib.request
import soundfile

import matplotlib.pyplot as plt
import numpy as np

import LBB.config as Config
import NB3.Sound.microphone as Microphone
import NB3.Sound.utilities as Utilities

SAMPLE_RATE = 48000
EAR_DISTANCE_M = 0.17
SPEED_OF_SOUND = 343.0

# Must match generate_localisation_chirp.py.
CHIRP_START_HZ = 400.0
CHIRP_END_HZ = 8000.0
CHIRP_DURATION_S = 0.080
CHIRP_FADE_S = 0.005

ITD_LOW_HZ = 400.0
ITD_HIGH_HZ = 1800.0
ILD_LOW_HZ = 2000.0
ILD_HIGH_HZ = 8000.0

# Extra audio retained on either side of the detected chirp.
WINDOW_PAD_S = 0.025

# These are diagnostic warnings, not hard rejection thresholds.
MIN_DETECTION_SCORE = 0.10
MIN_ITD_CORRELATION = 0.20

# Restrict chirp detection to a physically plausible interval after the
# scheduled playback time. This prevents unrelated room sounds elsewhere in
# the recording from winning the global matched-filter search.
DETECTION_SEARCH_START_S = 0.150
DETECTION_SEARCH_END_S = 0.750
NOMINAL_FIRST_CHIRP_S = 0.200
PROMINENCE_EXCLUSION_S = 0.050

# One localisation playback contains three identical chirps. Their known spacing
# acts as an acoustic code: unrelated room sounds must match all three events at
# the right intervals to win detection.
BURST_CHIRP_COUNT = 3
BURST_SPACING_S = 0.200
MAX_ITD_SPREAD_US = 75.0


@contextmanager
def suppress_native_stderr():
    """Temporarily suppress C-library stderr output (e.g. ALSA/JACK probing spam)."""
    stderr_fd = 2
    saved_stderr = os.dup(stderr_fd)
    try:
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), stderr_fd)
            yield
    finally:
        os.dup2(saved_stderr, stderr_fd)
        os.close(saved_stderr)


def make_reference_chirp():
    """Load the exact chirp samples from the WAV used for localisation playback."""
    chirp_path = os.path.join(
        Config.repo_path,
        "boxes/audio/signal-processing/python/generation/localisation_chirp.wav",
    )
    if not os.path.isfile(chirp_path):
        raise FileNotFoundError(f"Localisation chirp WAV not found: {chirp_path}")

    packet, fs = soundfile.read(chirp_path, dtype="float64", always_2d=True)
    if fs != SAMPLE_RATE:
        raise RuntimeError(
            f"Localisation chirp sample rate is {fs} Hz; expected {SAMPLE_RATE} Hz"
        )

    # The playback file is mono in normal use. If it is ever stored with more
    # than one channel, use the first channel explicitly rather than silently
    # averaging potentially different signals.
    packet = packet[:, 0]
    start = int(round(NOMINAL_FIRST_CHIRP_S * SAMPLE_RATE))
    stop = start + int(round(CHIRP_DURATION_S * SAMPLE_RATE))
    if stop > len(packet):
        raise RuntimeError(
            f"Localisation chirp WAV is too short to extract {CHIRP_DURATION_S:.3f} s "
            f"at t={NOMINAL_FIRST_CHIRP_S:.3f} s: {chirp_path}"
        )

    x = np.array(packet[start:stop], dtype=np.float64, copy=True)
    x -= np.mean(x)
    norm = np.linalg.norm(x)
    if norm <= 0.0:
        raise RuntimeError(f"Extracted localisation chirp is silent: {chirp_path}")
    x /= norm
    print(f"Matched-filter reference loaded from: {chirp_path}")
    return x


def fft_bandpass(x, low_hz, high_hz):
    x = np.asarray(x, dtype=np.float64)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spectrum[(freqs < low_hz) | (freqs > high_hz)] = 0
    return np.fft.irfft(spectrum, n=len(x))


def fft_valid_correlation(signal, reference):
    """
    Fast matched-filter correlation.

    Returns dot products for every location at which the full reference fits
    inside signal. Implemented with NumPy FFT so SciPy is not required.
    """
    signal = np.asarray(signal, dtype=np.float64)
    reference = np.asarray(reference, dtype=np.float64)

    n = len(signal) + len(reference) - 1
    nfft = 1 << (n - 1).bit_length()

    spectrum_signal = np.fft.rfft(signal, nfft)
    spectrum_ref = np.fft.rfft(reference[::-1], nfft)
    full = np.fft.irfft(spectrum_signal * spectrum_ref, nfft)[:n]

    start = len(reference) - 1
    stop = len(signal)
    return full[start:stop]


def matched_filter(signal, reference):
    """
    Locate the chirp and return a normalized detection score for each start time.
    """
    x = np.asarray(signal, dtype=np.float64)
    x = x - np.mean(x)

    dots = fft_valid_correlation(x, reference)

    # Normalize each dot product by the local signal energy. The reference has
    # unit norm, so this is approximately a correlation coefficient.
    sq = x * x
    csum = np.concatenate(([0.0], np.cumsum(sq)))
    m = len(reference)
    local_energy = csum[m:] - csum[:-m]
    denom = np.sqrt(np.maximum(local_energy, 1e-24))
    scores = dots / denom

    peak = int(np.argmax(np.abs(scores)))
    return peak, float(scores[peak]), scores


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

    a, b, c = y[i - 1], y[i], y[i + 1]
    denom = a - 2.0 * b + c

    if abs(denom) < 1e-12:
        return float(i)

    return i + float(np.clip(0.5 * (a - c) / denom, -1.0, 1.0))


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def analyse(recording, ear_distance, search_start_sample=None, search_end_sample=None):
    """Locate the complete three-chirp packet, then estimate ITD per chirp.

    Detection uses all three known chirp positions simultaneously in both ears.
    Each expected event is allowed a tiny local timing displacement (the physical
    interaural delay), but the 200 ms packet spacing is fixed.  This is much less
    likely to select an accidental single-chirp correlation peak.
    """
    left_full = recording[:, 0].astype(np.float64)
    right_full = recording[:, 1].astype(np.float64)
    left_full -= np.mean(left_full)
    right_full -= np.mean(right_full)
    reference = make_reference_chirp()

    if search_start_sample is None:
        search_start_sample = 0
    if search_end_sample is None:
        search_end_sample = len(left_full)
    search_start_sample = max(0, int(search_start_sample))
    search_end_sample = min(len(left_full), int(search_end_sample))

    spacing = int(round(BURST_SPACING_S * SAMPLE_RATE))
    burst_span = (BURST_CHIRP_COUNT - 1) * spacing + len(reference)
    if search_end_sample - search_start_sample < burst_span:
        raise RuntimeError("Burst detection search window is too short.")

    _, _, left_scores = matched_filter(left_full, reference)
    _, _, right_scores = matched_filter(right_full, reference)
    left_abs, right_abs = np.abs(left_scores), np.abs(right_scores)

    # The two ears can differ by up to ~24 samples.  When scoring the packet,
    # allow each ear to take its local maximum within that physically possible
    # displacement rather than sampling both ears at exactly the same index.
    max_lag = int(np.ceil(ear_distance / SPEED_OF_SOUND * SAMPLE_RATE))
    local_radius = max_lag + 2

    first_lo = search_start_sample
    first_hi = min(search_end_sample - burst_span + 1,
                   len(left_scores) - (BURST_CHIRP_COUNT - 1) * spacing,
                   len(right_scores) - (BURST_CHIRP_COUNT - 1) * spacing)
    if first_hi <= first_lo:
        raise RuntimeError("No complete three-chirp burst fits inside the search window.")

    candidates = np.arange(first_lo, first_hi, dtype=int)
    packet_curve = np.empty(len(candidates), dtype=np.float64)
    for ci, candidate in enumerate(candidates):
        evidence = []
        for k in range(BURST_CHIRP_COUNT):
            expected = candidate + k * spacing
            lo = max(0, expected - local_radius)
            hi = min(len(left_abs), expected + local_radius + 1)
            evidence.append(float(np.max(left_abs[lo:hi])))
            evidence.append(float(np.max(right_abs[lo:hi])))
        # RMS rewards a packet that is present repeatedly while still requiring
        # evidence from the complete six-event (3 chirps x 2 ears) pattern.
        packet_curve[ci] = float(np.sqrt(np.mean(np.square(evidence))))

    best_local = int(np.argmax(packet_curve))
    chirp_start = int(candidates[best_local])
    detection_score = float(packet_curve[best_local])

    # Compare the winning packet against the strongest independent alternative.
    # This tells us whether the best match is distinctive, not merely non-zero.
    exclusion = int(round(PROMINENCE_EXCLUSION_S * SAMPLE_RATE))
    independent = np.abs(candidates - chirp_start) > exclusion
    second_score = float(np.max(packet_curve[independent])) if np.any(independent) else 0.0
    detection_prominence = detection_score / max(second_score, 1e-12)

    chirp_starts = [chirp_start + k * spacing for k in range(BURST_CHIRP_COUNT)]
    pad = int(round(WINDOW_PAD_S * SAMPLE_RATE))
    itds, corrs, ilds, lag_samples_all = [], [], [], []
    representative_lags = representative_corr = None

    for start in chirp_starts:
        window_start = max(0, start - pad)
        window_end = min(len(left_full), start + len(reference) + pad)
        left = left_full[window_start:window_end]
        right = right_full[window_start:window_end]

        left_itd = fft_bandpass(left, ITD_LOW_HZ, ITD_HIGH_HZ)
        right_itd = fft_bandpass(right, ITD_LOW_HZ, ITD_HIGH_HZ)
        lags, corr = correlations_for_lags(left_itd, right_itd, max_lag)
        peak = int(np.argmax(corr))
        refined = refine_peak(corr, peak)
        lag_samples = lags[0] + refined
        itd = lag_samples / SAMPLE_RATE

        left_ild = fft_bandpass(left, ILD_LOW_HZ, ILD_HIGH_HZ)
        right_ild = fft_bandpass(right, ILD_LOW_HZ, ILD_HIGH_HZ)
        ild = 20.0 * np.log10((rms(left_ild) + 1e-12) / (rms(right_ild) + 1e-12))
        itds.append(itd)
        corrs.append(float(corr[peak]))
        ilds.append(float(ild))
        lag_samples_all.append(float(lag_samples))
        if representative_corr is None or corr[peak] > np.max(representative_corr):
            representative_lags, representative_corr = lags, corr

    itd = float(np.median(itds))
    lag_samples = itd * SAMPLE_RATE
    itd_spread_us = float((max(itds) - min(itds)) * 1e6)
    bearing = float(np.degrees(np.arcsin(np.clip(SPEED_OF_SOUND * itd / ear_distance, -1.0, 1.0))))

    detection_curve = np.full(len(left_scores), np.nan, dtype=np.float64)
    detection_curve[candidates] = packet_curve
    window_start = max(0, chirp_starts[0] - pad)
    window_end = min(len(left_full), chirp_starts[-1] + len(reference) + pad)

    return {
        "left_full": left_full, "right_full": right_full,
        "left": left_full[window_start:window_end], "right": right_full[window_start:window_end],
        "detection_curve": detection_curve, "chirp_start": chirp_start,
        "chirp_starts": chirp_starts, "window_start": window_start, "window_end": window_end,
        "detection_score": detection_score, "second_detection_score": second_score,
        "detection_prominence": detection_prominence,
        "search_start_sample": search_start_sample, "search_end_sample": search_end_sample,
        "lags": representative_lags, "corr": representative_corr,
        "lag_samples": lag_samples, "itd": itd, "bearing": bearing,
        "ild": float(np.median(ilds)), "itd_correlation": float(np.median(corrs)),
        "individual_itd_us": [v * 1e6 for v in itds], "individual_correlations": corrs,
        "individual_lag_samples": lag_samples_all, "itd_spread_us": itd_spread_us,
    }


def save_plot(result, path):
    left_full = result["left_full"]
    right_full = result["right_full"]

    t = np.arange(len(left_full)) / SAMPLE_RATE
    scale = max(np.max(np.abs(left_full)), np.max(np.abs(right_full)), 1.0)

    fig, ax = plt.subplots(3, 1, figsize=(10, 9))

    ax[0].plot(t, left_full / scale, label="Left ear", alpha=0.75)
    ax[0].plot(t, right_full / scale, label="Right ear", alpha=0.75)
    ax[0].axvspan(
        result["window_start"] / SAMPLE_RATE,
        result["window_end"] / SAMPLE_RATE,
        alpha=0.2,
        label="Analysis window",
    )
    ax[0].set(
        title="Full stereo recording and detected chirp window",
        xlabel="Time (s)",
        ylabel="Normalized amplitude",
    )
    ax[0].legend()
    ax[0].grid(True, alpha=0.3)

    detection_t = np.arange(len(result["detection_curve"])) / SAMPLE_RATE
    ax[1].plot(detection_t, np.abs(result["detection_curve"]))
    ax[1].axvline(
        result["chirp_start"] / SAMPLE_RATE,
        linestyle="--",
        label=f"Detected start = {result['chirp_start']/SAMPLE_RATE:.3f} s",
    )
    ax[1].set(
        title="Three-chirp packet detection",
        xlabel="Candidate chirp start time (s)",
        ylabel="Normalized match",
    )
    ax[1].legend()
    ax[1].grid(True, alpha=0.3)

    lag_us = result["lags"] / SAMPLE_RATE * 1e6
    ax[2].plot(lag_us, result["corr"])
    ax[2].axvline(
        result["itd"] * 1e6,
        linestyle="--",
        label=f"ITD = {result['itd']*1e6:+.1f} us",
    )
    ax[2].set(
        title=f"Windowed ITD correlation ({ITD_LOW_HZ:.0f}-{ITD_HIGH_HZ:.0f} Hz)",
        xlabel="Right-ear delay relative to left (us)",
        ylabel="Correlation",
    )
    ax[2].legend()
    ax[2].grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def post_binaural_result(server, port, fields, endpoint="/binaural_result"):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        f"http://{server}:{port}{endpoint}", data=body, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    p = argparse.ArgumentParser(
        description="Record a server-scheduled localisation chirp and estimate ToA/ITD/ILD."
    )
    p.add_argument("--post-roll", type=float, default=3.0,
                   help="Seconds to continue recording after scheduled emission (minimum 3.0 s).")
    p.add_argument("--ear-distance", type=float, default=EAR_DISTANCE_M)
    args = p.parse_args()

    server = os.environ.get("LBB_BINAURAL_SERVER")
    port = int(os.environ.get("LBB_BINAURAL_PORT", "8000"))
    pi_name = os.environ.get("LBB_BINAURAL_PI_NAME", "unknown")
    run_id = os.environ.get("LBB_BINAURAL_RUN_ID", "")
    if not server or not run_id:
        raise SystemExit(
            "Launch this test through the synchronized server/client binaural path."
        )

    save_path = os.path.join(
        f"{Config.repo_path}/boxes/audio/signal-processing/python/measurement",
        "binaural_chirp_test.png",
    )

    with suppress_native_stderr():
        input_device = Utilities.get_input_device_by_name("MAX")
        if input_device == -1:
            raise SystemExit('Input device "MAX" not found')

        buffer_size = SAMPLE_RATE // 10
        # Readiness gating can leave an early listener recording for many seconds; retain
        # enough audio that the beginning is not discarded before the burst arrives.
        max_samples = int(SAMPLE_RATE * 30.0)
        mic = Microphone.Microphone(
            input_device, 2, "int32", SAMPLE_RATE, buffer_size, max_samples
        )

    mic.gain = 10.0

    # Start capture immediately during the preparation phase.
    mic.start()
    record_start_epoch = time.time()

    # Tell the server capture is genuinely live BEFORE an emission epoch exists.
    post_binaural_result(server, port, {
        "id": pi_name,
        "run": run_id,
        "ready_at": f"{record_start_epoch:.9f}",
    }, endpoint="/binaural_ready")

    print(f"Recording start:       {record_start_epoch:.9f}")
    print("Waiting for server to arm localisation emission...")

    # Once every listener is READY, the server chooses a fresh future T. Polling
    # happens inside this already-recording process, so the client's 2 s command
    # polling interval can no longer make microphone capture miss the chirp.
    emit_at = None
    trigger_deadline = time.monotonic() + 20.0
    while time.monotonic() < trigger_deadline:
        reply = post_binaural_result(server, port, {
            "id": pi_name, "run": run_id,
        }, endpoint="/binaural_trigger")
        if reply.get("armed"):
            emit_at = float(reply["emit_at"])
            break
        time.sleep(0.05)
    if emit_at is None:
        mic.stop()
        raise SystemExit("Localisation emission was not armed within 20 seconds.")

    print(f"Scheduled chirp:       {emit_at:.9f}")
    print(f"Pre-roll:              {(emit_at-record_start_epoch)*1000:.1f} ms")
    print(f"Detection search:      T+{DETECTION_SEARCH_START_S*1000:.0f} to T+{DETECTION_SEARCH_END_S*1000:.0f} ms")

    # Keep the microphone running well beyond the complete 0.880 s localisation
    # packet.  A minimum of 3 s after T deliberately gives the audio backend,
    # acoustic propagation and any scheduling jitter ample margin.
    post_roll_s = max(3.0, float(args.post_roll))
    stop_at = emit_at + post_roll_s
    try:
        while time.time() < stop_at:
            time.sleep(0.005)

        # mic.sound is preallocated to max_samples. Only [:valid_samples] contains
        # captured audio until that buffer fills. Take one locked snapshot and use
        # this exact array for both analysis and WAV export.
        with mic.mutex:
            valid_samples = int(mic.valid_samples)
            recording = np.copy(mic.sound[:valid_samples, :])
    finally:
        mic.stop()

    if recording.ndim != 2 or recording.shape[1] < 2:
        raise SystemExit(f"Expected stereo audio; got {recording.shape}")

    record_stop_epoch = record_start_epoch + len(recording) / SAMPLE_RATE
    post_t_recorded_s = record_stop_epoch - emit_at
    packet_end_after_t_s = (
        NOMINAL_FIRST_CHIRP_S
        + (BURST_CHIRP_COUNT - 1) * BURST_SPACING_S
        + CHIRP_DURATION_S
    )
    complete_packet = post_t_recorded_s >= packet_end_after_t_s

    print(f"Recording stop:        {record_stop_epoch:.9f}")
    print(f"Post-T recorded:       {post_t_recorded_s:.3f} s")
    print(f"Packet requires:       {packet_end_after_t_s:.3f} s after T")
    print(f"Complete packet capture: {'YES' if complete_packet else 'NO'}")
    if not complete_packet:
        raise SystemExit(
            "Localisation recording ended before the complete three-chirp packet "
            f"could be captured ({post_t_recorded_s:.3f} s available; "
            f"{packet_end_after_t_s:.3f} s required)."
        )

    # Sanity-check the exact snapshot that will be analysed and saved.
    print("Raw capture:")
    print(f"  shape: {recording.shape}")
    print(f"  dtype: {recording.dtype}")
    for ch, name in enumerate(("L", "R")):
        x = recording[:, ch].astype(np.float64, copy=False)
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        print(
            f"  {name}: min={np.min(x):+.8f}, max={np.max(x):+.8f}, "
            f"rms={rms:.8f}, nonzero={np.count_nonzero(x)}/{x.size}"
        )

    # Save the same normalized float32 samples used by analyse(). soundfile
    # performs the float [-1, 1] -> signed PCM_32 conversion correctly.
    safe_run_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
    safe_pi_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in pi_name)
    raw_wav_path = os.path.expanduser(
        f"~/binaural_raw_{safe_run_id}_{safe_pi_name}.wav"
    )
    soundfile.write(
        raw_wav_path, recording[:, :2], SAMPLE_RATE, subtype="PCM_32"
    )
    print(f"Raw stereo WAV saved:  {raw_wav_path}")

    # Convert the matched-filter sample offset in the captured snapshot into the
    # same Chrony-disciplined epoch used by the server.
    # Convert the scheduled playback epoch into recording-relative samples and
    # search only in the physically plausible first-chirp interval. The transmitted
    # WAV contains 200 ms of leading silence; the remaining width allows for
    # speaker/backend latency while excluding impossible early matches.
    scheduled_sample = int(round((emit_at - record_start_epoch) * SAMPLE_RATE))
    search_start_sample = scheduled_sample + int(round(DETECTION_SEARCH_START_S * SAMPLE_RATE))
    search_end_sample = scheduled_sample + int(round(DETECTION_SEARCH_END_S * SAMPLE_RATE))

    burst_span_samples = int(round((BURST_CHIRP_COUNT - 1) * BURST_SPACING_S * SAMPLE_RATE)) + int(round(CHIRP_DURATION_S * SAMPLE_RATE))
    available_start = max(0, search_start_sample)
    available_end = min(len(recording), search_end_sample)
    if available_end - available_start < burst_span_samples:
        raise SystemExit(
            "Localisation recording does not contain a complete post-T triple-chirp "
            f"search window (pre-roll={(emit_at-record_start_epoch)*1000:.1f} ms)."
        )

    result = analyse(
        recording,
        args.ear_distance,
        search_start_sample=search_start_sample,
        search_end_sample=search_end_sample,
    )
    arrival_epoch = record_start_epoch + result["chirp_start"] / SAMPLE_RATE
    toa_s = arrival_epoch - emit_at
    apparent_distance_m = SPEED_OF_SOUND * toa_s

    print("\nResults")
    print("-------")
    print(f"Detected arrival:      {arrival_epoch:.9f}")
    print(f"Apparent ToF:          {toa_s*1000:+.3f} ms")
    print(f"Apparent distance:     {apparent_distance_m:+.3f} m")
    print(f"Detection score:       {result['detection_score']:.3f}")
    print(f"Second-best packet:    {result['second_detection_score']:.3f}")
    print(f"Packet prominence:     {result['detection_prominence']:.2f}x")
    print(f"Output latency est.:   {(toa_s-NOMINAL_FIRST_CHIRP_S)*1000:+.1f} ms")
    print(f"ITD:                   {result['itd']*1e6:+.1f} us")
    print(f"Sample lag:            {result['lag_samples']:+.2f} samples")
    print(f"ILD (L/R):             {result['ild']:+.2f} dB")
    print(f"ITD bearing:           {result['bearing']:+.1f} degrees")
    print(f"ITD correlation:       {result['itd_correlation']:.3f}")
    print("Per-chirp ITDs:        " + ", ".join(f"{v:+.1f} us" for v in result["individual_itd_us"]))
    print(f"ITD spread:            {result['itd_spread_us']:.1f} us")

    warnings = []
    if result["detection_score"] < MIN_DETECTION_SCORE:
        warnings.append("weak chirp detection")
    if result["itd_correlation"] < MIN_ITD_CORRELATION:
        warnings.append("weak binaural correlation")
    if result["itd_spread_us"] > MAX_ITD_SPREAD_US:
        warnings.append(f"inconsistent three-chirp ITDs ({result['itd_spread_us']:.1f} us spread)")
    physical_max = args.ear_distance / SPEED_OF_SOUND
    if abs(result["itd"]) > 0.95 * physical_max:
        warnings.append("ITD is very close to the physical limit")
    if toa_s <= 0:
        warnings.append("non-positive apparent ToF")

    confidence = "low" if warnings else "ok"
    if warnings:
        print("\nWARNING: " + "; ".join(warnings) + ".")
    else:
        print("\nMeasurement confidence: OK")

    save_plot(result, save_path)

    fields = {
        "id": pi_name,
        "run": run_id,
        "scheduled_emit_epoch": f"{emit_at:.9f}",
        "record_start_epoch": f"{record_start_epoch:.9f}",
        "arrival_epoch": f"{arrival_epoch:.9f}",
        "toa_ms": f"{toa_s*1000:.6f}",
        "apparent_distance_m": f"{apparent_distance_m:.6f}",
        "itd_us": f"{result['itd']*1e6:.6f}",
        "lag_samples": f"{result['lag_samples']:.6f}",
        "ild_db": f"{result['ild']:.6f}",
        "bearing_deg": f"{result['bearing']:.6f}",
        "detection_score": f"{result['detection_score']:.6f}",
        "detection_prominence": f"{result['detection_prominence']:.6f}",
        "itd_correlation": f"{result['itd_correlation']:.6f}",
        "itd_spread_us": f"{result['itd_spread_us']:.6f}",
        "individual_itd_us": ",".join(f"{v:.6f}" for v in result["individual_itd_us"]),
        "confidence": confidence,
        "warnings": "; ".join(warnings),
    }
    reply = post_binaural_result(server, port, fields)
    print(f"Result sent to server: {bool(reply.get('ok'))}")
    print(f"Diagnostic plot saved to:\n  {save_path}")


if __name__ == "__main__":
    main()
