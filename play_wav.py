
import os
import sys
import time
import argparse

import numpy as np
import soundfile as sf

import LBB.config as Config
import NB3.Sound.speaker as Speaker
import NB3.Sound.utilities as Utilities


# ============================================================
# SETTINGS
# ============================================================

# Change this to the WAV file you want to play.
WAV_FILE = (
    f"{Config.repo_path}/boxes/audio/signal-processing/"
    f"python/generation/Choir.wav"
)

# Speaker settings
OUTPUT_DEVICE_NAME = "MAX"
NUM_CHANNELS = 2
SAMPLE_RATE = 48000
BUFFER_SIZE = int(SAMPLE_RATE / 10)


# ============================================================
# COMMAND-LINE ARGUMENTS
# ============================================================

parser = argparse.ArgumentParser(
    description="Play a WAV file through the NB3 speaker system."
)

parser.add_argument(
    "-v",
    "--volume",
    type=float,
    default=100,
    help="Volume from 0-100. 100 = maximum, 0 = silent."
)

args = parser.parse_args()


# ============================================================
# CHECK VOLUME
# ============================================================

if not 0 <= args.volume <= 100:
    parser.error("Volume must be between 0 and 100.")


# ============================================================
# CONVERT VOLUME TO LINEAR GAIN
# ============================================================

# Map:
#
#     100 ->   0 dB
#      50 -> -40 dB
#      25 -> -60 dB
#      10 -> -72 dB
#       5 -> -76 dB
#       1 -> -79.2 dB
#       0 -> silent
#
# This gives much finer control at low volumes than simply
# multiplying the samples by volume / 100.

if args.volume == 0:
    gain = 0.0
else:
    min_gain = 0.05
    max_gain = 4.0
    x = args.volume / 100.0
    gain = min_gain * (max_gain / min_gain) ** x


#print(f"Volume setting: {args.volume:.1f}%")
#print(f"Additional gain: {gain:.8f}")


# ============================================================
# CHECK WAV FILE
# ============================================================

if not os.path.exists(WAV_FILE):
    raise FileNotFoundError(
        f"WAV file not found:\n{WAV_FILE}"
    )


# ============================================================
# LOAD WAV
# ============================================================

#print(f"Loading: {WAV_FILE}")

audio, wav_sample_rate = sf.read(
    WAV_FILE,
    dtype="float32",
    always_2d=True
)

#print(f"Input sample rate: {wav_sample_rate} Hz")
#print(f"Input channels: {audio.shape[1]}")
#print(f"Number of samples: {audio.shape[0]}")


# ============================================================
# CHECK SAMPLE RATE
# ============================================================

if wav_sample_rate != SAMPLE_RATE:
    raise RuntimeError(
        f"WAV sample rate is {wav_sample_rate} Hz, "
        f"but speaker is configured for {SAMPLE_RATE} Hz.\n"
        f"Please convert the WAV to {SAMPLE_RATE} Hz first."
    )


# ============================================================
# CHECK CHANNELS
# ============================================================

if audio.shape[1] == 1:

    # Convert mono -> stereo
    audio = np.repeat(audio, 2, axis=1)

elif audio.shape[1] != NUM_CHANNELS:

    raise RuntimeError(
        f"WAV has {audio.shape[1]} channels, "
        f"but speaker expects {NUM_CHANNELS}."
    )


# ============================================================
# APPLY VOLUME
# ============================================================

audio *= gain


# ============================================================
# SAFETY CLIP
# ============================================================

audio = np.clip(audio, -1.0, 1.0)


# ============================================================
# INITIALIZE SPEAKER
# ============================================================

#print()
#print("Available audio devices:")

#Utilities.list_devices()

output_device = Utilities.get_output_device_by_name(
    OUTPUT_DEVICE_NAME
)

if output_device == -1:
    raise RuntimeError(
        f"Output device '{OUTPUT_DEVICE_NAME}' not found."
    )


speaker = Speaker.Speaker(
    output_device,
    NUM_CHANNELS,
    "int32",
    SAMPLE_RATE,
    BUFFER_SIZE
)


# ============================================================
# PLAY WAV
# ============================================================

duration = len(audio) / SAMPLE_RATE

#print()
#print(f"Playing: {WAV_FILE}")
#print(f"Volume: {args.volume:.1f}%")
#print(f"Duration: {duration:.2f} seconds")
#print()

# Audio checkpoint mode is enabled only for play_wav.py. The script receives
# two inherited file descriptors through these environment variables.
AUDIO_READY_FD = int(os.environ.get("LBB_AUDIO_READY_FD", "-1"))
AUDIO_GO_FD = int(os.environ.get("LBB_AUDIO_GO_FD", "-1"))

speaker.start()

if AUDIO_READY_FD >= 0:
    os.write(AUDIO_READY_FD, b"READY\n")
    os.close(AUDIO_READY_FD)

if AUDIO_GO_FD >= 0:
    os.read(AUDIO_GO_FD, 1)
    go_received_at = time.time()
    os.close(AUDIO_GO_FD)
else:
    go_received_at = None

if go_received_at is not None:
    print(f"AUDIO_GO_RECEIVED: {go_received_at:.9f}", flush=True)

try:

    # This is the audible-start checkpoint: the backend and device are ready,
    # and the next operation submits the waveform.
    audio_checkpoint_at = time.time()
    print(f"AUDIO_CHECKPOINT: {audio_checkpoint_at:.9f}", flush=True)
    speaker.write(audio)

    while speaker.is_playing():
        time.sleep(0.01)

finally:

    speaker.stop()

print("Finished.")

