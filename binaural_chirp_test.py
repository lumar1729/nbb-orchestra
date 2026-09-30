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
from scipy.signal import butter, sosfiltfilt, fftconvolve, find_peaks

import numpy as np

import LBB.config as Config
import NB3.Sound.microphone as Microphone
import NB3.Sound.utilities as Utilities

SAMPLE_RATE = 48000
EAR_DISTANCE_M = 0.183123  # CAD: acoustic-port to acoustic-port
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
DETECTION_SEARCH_START_S = -0.100
DETECTION_SEARCH_END_S = 1.500
NOMINAL_FIRST_CHIRP_S = 0.200
PROMINENCE_EXCLUSION_S = 0.050
PACKET_TIMING_TOLERANCE_S = 0.030
PACKET_TIMING_SCALE_S = 0.020
ITD_WINDOW_BEFORE_S = 0.003
ITD_WINDOW_DURATION_S = 0.086  # 3 ms pre-roll + full 80 ms chirp + small tail
GCC_INTERPOLATION = 16
ITD_LIMIT_MARGIN = 1.00  # path-length difference cannot exceed acoustic-port spacing
ITD_CONSENSUS_RADIUS_US = 90.0

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


def bandpass(x, low_hz=350.0, high_hz=9000.0):
    """Zero-phase Butterworth band-pass used by the validated offline detector."""
    x = np.asarray(x, dtype=np.float64)
    high_hz = min(float(high_hz), SAMPLE_RATE * 0.49)
    sos = butter(4, [float(low_hz), high_hz], btype="bandpass",
                 fs=SAMPLE_RATE, output="sos")
    return sosfiltfilt(sos, x, axis=0)


def make_reference_chirp():
    """Extract the first active 80-ms chirp exactly as in the offline test."""
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
    mono = packet.mean(axis=1)
    smooth_n = max(1, int(0.004 * SAMPLE_RATE))
    energy = fftconvolve(mono * mono, np.ones(smooth_n) / smooth_n, mode="same")
    active = np.flatnonzero(energy > energy.max() * 0.03)
    if not len(active):
        raise RuntimeError(f"No active chirp found in localisation WAV: {chirp_path}")

    start = max(0, int(active[0]) - int(0.004 * SAMPLE_RATE))
    n = int(round(CHIRP_DURATION_S * SAMPLE_RATE))
    if start + n > len(mono):
        raise RuntimeError("Localisation WAV is too short to extract the reference chirp.")
    reference = bandpass(mono[start:start+n])
    reference -= reference.mean()
    reference /= np.linalg.norm(reference) + 1e-15
    print(f"Matched-filter reference loaded from: {chirp_path}")
    return reference, start / SAMPLE_RATE


def normalized_matched_filter(signal, reference):
    """Normalized FFT matched-filter score at every possible chirp start."""
    x = np.asarray(signal, dtype=np.float64)
    n = len(reference)
    corr = fftconvolve(x, reference[::-1], mode="full")[n-1:n-1+len(x)]
    energy = fftconvolve(x*x, np.ones(n), mode="full")[n-1:n-1+len(x)]
    return corr / (np.sqrt(np.maximum(energy, 1e-20)) *
                   (np.linalg.norm(reference) + 1e-15))


def detect_packet(score, search_start_sample, search_end_sample):
    """Find a three-chirp acoustic code, using the validated offline algorithm."""
    s = np.abs(score)
    spacing = int(round(BURST_SPACING_S * SAMPLE_RATE))
    tolerance = int(round(PACKET_TIMING_TOLERANCE_S * SAMPLE_RATE))
    chirp_n = int(round(CHIRP_DURATION_S * SAMPLE_RATE))

    lo = max(0, int(search_start_sample))
    hi = min(len(s) - 2 * spacing - chirp_n, int(search_end_sample))
    if hi <= lo:
        raise RuntimeError("No complete three-chirp packet fits inside the search window.")

    peaks, _ = find_peaks(s[lo:hi], distance=int(round(0.035 * SAMPLE_RATE)))
    peaks = peaks + lo
    if not len(peaks):
        raise RuntimeError("No chirp candidates found in the localisation search window.")
    peaks = peaks[np.argsort(s[peaks])[::-1]][:100]

    candidates = []
    for p0 in peaks:
        starts = [int(p0)]
        scores = [float(s[p0])]
        for k in (1, 2):
            target = int(p0) + k * spacing
            a = max(0, target - tolerance)
            b = min(len(s), target + tolerance + 1)
            if b <= a:
                break
            p = a + int(np.argmax(s[a:b]))
            starts.append(p)
            scores.append(float(s[p]))
        if len(starts) != BURST_CHIRP_COUNT:
            continue
        timing_error = (abs((starts[1]-starts[0]) / SAMPLE_RATE - BURST_SPACING_S) +
                        abs((starts[2]-starts[1]) / SAMPLE_RATE - BURST_SPACING_S))
        strength = float(np.prod(np.maximum(scores, 1e-12)) ** (1.0/3.0))
        objective = strength * math.exp(-timing_error / PACKET_TIMING_SCALE_S)
        candidates.append((objective, strength, starts, scores))

    if not candidates:
        raise RuntimeError("No complete three-chirp packet found.")
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0]
    second = next((c for c in candidates[1:]
                   if abs(c[2][0] - best[2][0]) > int(PROMINENCE_EXCLUSION_S*SAMPLE_RATE)),
                  None)
    second_score = float(second[1]) if second else 0.0
    return best[1], second_score, best[2], best[3]


def gcc_phat_curve(left, right, max_itd_s, interp=GCC_INTERPOLATION):
    """Return the GCC-PHAT magnitude over the physically possible ITD range.

    Positive lag means the right channel is delayed.  The returned curve is
    normalized to unit peak so curves from all three chirps can be combined.
    """
    nfft = 1 << int(np.ceil(np.log2(len(left) + len(right))))
    left_fft = np.fft.rfft(left, nfft)
    right_fft = np.fft.rfft(right, nfft)
    cross = left_fft * np.conj(right_fft)
    cross /= np.maximum(np.abs(cross), 1e-15)
    cc = np.fft.irfft(cross, nfft * interp)
    max_shift = int(np.floor(max_itd_s * SAMPLE_RATE * interp))
    local = np.r_[cc[-max_shift:], cc[:max_shift+1]]
    # gcc_phat used the opposite FFT-shift sign; preserve the public convention
    # that positive ITD means the right channel is delayed.
    lags_s = -np.arange(-max_shift, max_shift + 1, dtype=float) / (interp * SAMPLE_RATE)
    order = np.argsort(lags_s)
    lags_s = lags_s[order]
    mag = np.abs(local)[order]
    mag /= np.max(mag) + 1e-15
    return lags_s, mag


def _parabolic_peak(lags_s, curve, j):
    """Refine one sampled correlation peak with a three-point parabola."""
    frac = 0.0
    if 0 < j < len(curve) - 1:
        denom = curve[j-1] - 2.0*curve[j] + curve[j+1]
        if abs(denom) > 1e-15:
            frac = 0.5 * (curve[j-1] - curve[j+1]) / denom
            frac = float(np.clip(frac, -1.0, 1.0))
    step = lags_s[1] - lags_s[0] if len(lags_s) > 1 else 0.0
    return float(lags_s[j] + frac * step)


def _aligned_corr(left, right, itd):
    """Ordinary normalized correlation after aligning by the requested ITD."""
    lag = int(round(itd * SAMPLE_RATE))
    if lag >= 0:
        l = left[:-lag] if lag else left
        r = right[lag:] if lag else right
    else:
        k = -lag
        l, r = left[k:], right[:-k]
    return float(np.dot(l, r) / (np.linalg.norm(l)*np.linalg.norm(r) + 1e-15))


def itd_curve_at(recording, start, ear_distance):
    """Compute one chirp's GCC-PHAT curve over the physical ITD interval."""
    a = max(0, int(start) - int(round(ITD_WINDOW_BEFORE_S * SAMPLE_RATE)))
    b = min(len(recording), a + int(round(ITD_WINDOW_DURATION_S * SAMPLE_RATE)))
    z = bandpass(recording[a:b, :2])
    left = z[:, 0] - z[:, 0].mean()
    right = z[:, 1] - z[:, 1].mean()
    window = np.hanning(len(left))
    max_itd_s = ear_distance / SPEED_OF_SOUND * ITD_LIMIT_MARGIN
    lags_s, curve = gcc_phat_curve(left * window, right * window, max_itd_s)
    return lags_s, curve, left, right


def consensus_itd(recording, starts, ear_distance):
    """Estimate one ITD jointly from all three repeated chirps.

    Each chirp contributes its complete GCC-PHAT curve.  Their geometric mean
    rewards a delay that is supported by every repetition and suppresses a
    strong reflection/sidelobe that appears in only one chirp.  Individual
    diagnostics are then measured at the strongest local peak near that joint
    solution rather than allowing unrelated peaks to determine the median.
    """
    items = [itd_curve_at(recording, start, ear_distance) for start in starts]
    lags_s = items[0][0]
    curves = np.vstack([item[1] for item in items])
    consensus_curve = np.exp(np.mean(np.log(np.maximum(curves, 1e-12)), axis=0))
    j = int(np.argmax(consensus_curve))
    itd = _parabolic_peak(lags_s, consensus_curve, j)

    radius = ITD_CONSENSUS_RADIUS_US * 1e-6
    individual = []
    correlations = []
    for curve, (_, _, left, right) in zip(curves, items):
        mask = np.abs(lags_s - itd) <= radius
        indices = np.flatnonzero(mask)
        jj = int(indices[np.argmax(curve[indices])]) if len(indices) else j
        local_itd = _parabolic_peak(lags_s, curve, jj)
        individual.append(local_itd)
        correlations.append(_aligned_corr(left, right, local_itd))

    return itd, individual, correlations, lags_s, consensus_curve


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def analyse(recording, ear_distance, search_start_sample=None, search_end_sample=None):
    """Detect the coded triple chirp, then estimate an independent ITD per chirp.

    This is the same detection/ITD method validated offline on the saved stereo
    recordings: broadband normalized matched filtering on the channel mean,
    explicit 200-ms three-event pattern matching, then short-window GCC-PHAT.
    """
    x = np.asarray(recording[:, :2], dtype=np.float64)
    reference, reference_start_s = make_reference_chirp()
    filtered = bandpass(x)
    score = normalized_matched_filter(filtered.mean(axis=1), reference)

    if search_start_sample is None:
        search_start_sample = 0
    if search_end_sample is None:
        search_end_sample = len(score)
    detection_score, second_score, chirp_starts, chirp_scores = detect_packet(
        score, search_start_sample, search_end_sample)
    detection_prominence = detection_score / max(second_score, 1e-12)

    itd, itds, corrs, consensus_lags_s, consensus_curve = consensus_itd(
        x, chirp_starts, ear_distance)
    lag_samples = itd * SAMPLE_RATE
    itd_spread_us = float(np.ptp(itds) * 1e6)
    bearing = float(np.degrees(np.arcsin(np.clip(
        SPEED_OF_SOUND * itd / ear_distance, -1.0, 1.0))))

    # Preserve ILD as a diagnostic, using the whole detected packet region.
    pad = int(round(WINDOW_PAD_S * SAMPLE_RATE))
    window_start = max(0, chirp_starts[0] - pad)
    window_end = min(len(x), chirp_starts[-1] + len(reference) + pad)
    left_ild = bandpass(x[window_start:window_end, 0], ILD_LOW_HZ, ILD_HIGH_HZ)
    right_ild = bandpass(x[window_start:window_end, 1], ILD_LOW_HZ, ILD_HIGH_HZ)
    ild = 20.0 * np.log10((rms(left_ild)+1e-12)/(rms(right_ild)+1e-12))

    return {
        "chirp_start": chirp_starts[0],
        "chirp_starts": chirp_starts, "chirp_scores": chirp_scores,
        "reference_start_s": reference_start_s,
        "window_start": window_start, "window_end": window_end,
        "detection_score": detection_score, "second_detection_score": second_score,
        "detection_prominence": detection_prominence,
        "search_start_sample": search_start_sample, "search_end_sample": search_end_sample,
        "lag_samples": lag_samples, "itd": itd, "bearing": bearing,
        "ild": float(ild), "itd_correlation": float(np.median(corrs)),
        "individual_itd_us": [v*1e6 for v in itds], "individual_correlations": corrs,
        "individual_lag_samples": [v*SAMPLE_RATE for v in itds],
        "itd_spread_us": itd_spread_us,
    }

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
        # captured audio until that buffer fills. Take one locked snapshot for analysis.
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
    print(f"Output latency est.:   {(toa_s-result['reference_start_s'])*1000:+.1f} ms")
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


if __name__ == "__main__":
    main()
