# Raspberry Pi Message & Command Client (multi-Pi + live messaging!)
# - Connects to the "Message Board Server" running on your laptop
# - Identifies itself with a NAME (its hostname by default) so that many Pis
#   can connect at once, each getting only the commands meant for it
# - AFTER connecting you can type messages any time: press Enter and they
#   instantly appear on the PUBLIC message board on the website
# - Meanwhile a background thread runs commands PRIVATELY (quietly: nothing
#   about commands is ever printed here -- that stays on the laptop's
#   terminal + command feed) and shows only board messages ADDRESSED to this
#   Pi: lines starting "@All" (everyone) or "@<this-pi's-name>" (just us).
#   So address messages like "@All Hello everyone!" to be seen on the Pis!
# - The same poller measures lag BOTH ways for the server's left panel:
#   Pi->server is timed HERE (how long each GET /get_command takes, sent as
#   ?prtt=MS); server->Pi is timed by the SERVER via magic "__ping__"
#   commands, which this client answers INSTANTLY (never executed as shell:
#   intercepted before the whitelist) with POST /pong.
# - Clock sync + SCHEDULED runs: the Windows server clock is disciplined by
#   NTP through the Windows Time Service. This Pi's UTC clock is disciplined by
#   chrony. The Pi waits for the server's intended UTC epoch directly and
#   reports actual_executed_at - intended_at, with chrony quality indicators.
# - Uses ONLY the Python standard library (no external packages needed)
#
# Run it on the Pi:
#   python pi_message_client.py <laptop_ip> [port] [--name mypi] [intro_message]
#   e.g.  python pi_message_client.py 192.168.1.42
# Then type messages here to post them on the website, and type commands on
# the website for this Pi to run. Type "quit" here (or Control + C) to exit.
import re
import urllib.request
import urllib.parse
import json
import shlex      # Safely splits a command string into words
import subprocess
import threading  # So message typing and command polling run at the same time
import socket
import sys
import time       # perf_counter times each poll for the Pi->server lag
import base64
import http.server
import socketserver

import os
import shutil
import wave

# Make emoji/text output safe on every platform (Windows consoles default
# to encodings like cp1252 which cannot represent emoji characters)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass  # Older Python versions or unusual output streams

# ----------------- Configuration -----------------
POLL_SECONDS = 2      # How often the Pi asks the server "any commands for me?"
COMMAND_TIMEOUT = 600  # Max seconds a command may run before we give up on it
SYNC_SPIN_MARGIN = 0.05  # Last 50ms before a scheduled run are a BUSY-SPIN:
# time.sleep() overshoots by up to a few ms on Linux and ~15ms on Windows, so
# the final approach must not sleep. 50ms of spinning costs nothing and keeps
# the execution time within a millisecond or so of the target.

# Working directory used for commands sent from the server. It is persistent
# for the lifetime of the client process; `cd` updates it.
COMMAND_CWD = None

# Kill-all control channel. The laptop POSTs to message-board port + 1 so a
# running command can be terminated even while the normal command poller is
# blocked waiting for that process to finish.
command_processes = set()
command_process_lock = threading.Lock()


def register_process(process):
    """Register a child process so the independent kill listener can stop it."""
    with command_process_lock:
        command_processes.add(process)


def unregister_process(process):
    with command_process_lock:
        command_processes.discard(process)


def terminate_command_process(process):
    """Terminate one child and, on Linux, its complete process group."""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), 15)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass


def kill_command_processes():
    with command_process_lock:
        processes = list(command_processes)
    for process in processes:
        terminate_command_process(process)
    return len(processes)


class ControlHandler(http.server.BaseHTTPRequestHandler):
    """Small independent HTTP endpoint used only for the server's Kill All."""

    def do_POST(self):
        if self.path == "/kill_all":
            count = kill_command_processes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"ok": True, "killed": count}).encode("utf-8")
            )
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass


def serve_control_port(base_port):
    """Listen on base_port + 1 without blocking the normal message client."""
    try:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer(("", base_port + 1), ControlHandler) as server:
            server.serve_forever()
    except OSError as error:
        print(
            f"WARNING: Kill-all control listener could not bind to "
            f"port {base_port + 1}: {error}",
            flush=True,
        )
# a remote shell for strangers on the network. Add more if you need them!
ALLOWED_COMMANDS = [
    "uptime", "hostname", "whoami", "date", "ls", "pwd",
    "free", "df", "uname", "cat", "vcgencmd", "python", "python3", "sudo", "cd",
    "Activate", "source",
]


# Accept a Python executable by either its command name or an absolute/relative
# path whose final component is python, python3, or python3.x.
PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:3(?:\\.\\d+)?)?$", re.IGNORECASE)


def is_allowed_program(program):
    """Return whether a command executable is on the client allowlist."""
    if program in ALLOWED_COMMANDS:
        return True
    return bool(PYTHON_EXECUTABLE_RE.fullmatch(
        re.split(r"[/\\\\]", program)[-1]))


# ----------------- Helper: figure out the laptop's address -----------------
def resolve_host(host):
    # If the user passed "localhost", translate it into a real address,
    # because "localhost" on the Pi would mean the Pi itself, not the laptop!
    return socket.gethostbyname(host)

# ----------------- Helper: POST something to the server -----------------
def http_post(server_ip, port, path, fields):
    # Encode the values as form data (same as an HTML form would send)
    data = urllib.parse.urlencode(fields).encode("utf-8")
    url = f"http://{server_ip}:{port}{path}"
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")

# ----------------- Helper: inspect chrony's disciplined clock -----------------
def read_chrony_status():
    """Read the kernel-advertised time-sync state exposed by chronyd.

    `synchronized` is deliberately conservative: chronyd must have a leap
    status of Normal, a valid stratum, and a selected source. Root dispersion is
    retained as the timing uncertainty shown next to execution error.
    """
    result = {"available": False, "synchronized": False, "leap": None,
              "stratum": None, "root_dispersion": None, "skew": None,
              "offset": None, "source": None, "error": None}
    try:
        tracking = subprocess.run(
            ["chronyc", "-n", "tracking"], capture_output=True, text=True,
            timeout=2, check=False)
        sources = subprocess.run(
            ["chronyc", "-n", "sources", "-v"], capture_output=True,
            text=True, timeout=2, check=False)
        if tracking.returncode or sources.returncode:
            result["error"] = (tracking.stderr or sources.stderr).strip()
            return result
        values = {}
        for line in tracking.stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip().lower()] = value.strip()
        result.update(available=True, leap=values.get("leap status"),
                      stratum=values.get("stratum"),
                      root_dispersion=values.get("root dispersion"),
                      skew=values.get("skew"), offset=values.get("system time"))
        for number in ("stratum",):
            if result[number] is not None:
                match = re.search(r"-?\d+", result[number])
                result[number] = int(match.group()) if match else None
        for number in ("root_dispersion", "skew"):
            if result[number] is not None:
                match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", result[number])
                result[number] = float(match.group()) if match else None
        if result["offset"] is not None:
            offset_text = result["offset"]
            match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", offset_text)
            if match:
                result["offset"] = float(match.group()) * 1000
                if "slow" in offset_text.lower():
                    result["offset"] = -abs(result["offset"])
            else:
                result["offset"] = None

        selected = []

        for line in sources.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue

            marker = fields[0]

            # ^* = selected best source
            # ^+ = usable/combined source
            if len(marker) >= 2 and marker[1] in "*+":
                selected.append(fields)

        if selected:
            # The first column is the state marker; the second is the source.
            result["source"] = selected[0][1]
        result["synchronized"] = (
            result["leap"] == "Normal" and
            (result["stratum"] is None or result["stratum"] > 0) and
            result["source"] is not None
        )
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


# ----------------- One-time chrony setup -----------------
CHRONY_CONFIG = "/etc/chrony/chrony.conf"
CHRONY_MARKER = "# managed by pi_message_client.py"
CHRONY_SETUP_TIMEOUT = 20.0


def setup_chrony(server_host):
    """Configure this Pi to use the Windows host as its LAN NTP source.

    This is deliberately explicit and one-time. It refuses to run without root,
    preserves the original configuration, and never removes other sources.
    """
    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError(
            "chrony setup must run as root: sudo python3 pi_message_client.py "
            f"{server_host} --setup-chrony")
    if not os.path.exists(CHRONY_CONFIG):
        raise FileNotFoundError(f"chrony configuration not found: {CHRONY_CONFIG}")
    if not shutil.which("chronyc") or not shutil.which("systemctl"):
        raise RuntimeError("install chrony first: sudo apt install chrony")

    backup = f"{CHRONY_CONFIG}.pi-message-board.bak"
    if not os.path.exists(backup):
        shutil.copy2(CHRONY_CONFIG, backup)
    with open(CHRONY_CONFIG, encoding="utf-8") as config_file:
        config = config_file.read()
    config = re.sub(
        rf"(?ms)^\s*{re.escape(CHRONY_MARKER)}:.*?^\s*{re.escape(CHRONY_MARKER)} end\s*\n?",
        "", config)
    source_line = f"server {server_host} iburst minpoll 4 maxpoll 6"
    if source_line not in config:
        config += (
            f"\n{CHRONY_MARKER}: Windows message-board server\n"
            f"{source_line}\n"
            f"{CHRONY_MARKER} end\n")
    with open(CHRONY_CONFIG, "w", encoding="utf-8") as config_file:
        config_file.write(config)
    try:
        subprocess.run(["systemctl", "restart", "chrony"], check=True,
                       capture_output=True, text=True, timeout=10)
        subprocess.run(["chronyc", "online"], check=True,
                       capture_output=True, text=True, timeout=5)
        # Correct a large startup offset only during explicit setup.
        subprocess.run(["chronyc", "makestep"], check=False,
                       capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not start chrony: {error}") from error

    deadline = time.monotonic() + CHRONY_SETUP_TIMEOUT
    while time.monotonic() < deadline:
        status = read_chrony_status()
        if status["synchronized"]:
            return status
        time.sleep(1)
    status = read_chrony_status()
    raise RuntimeError(
        "chrony did not select a source within 20 seconds. "
        f"last error: {status.get('error') or 'no source selected'}. "
        "Check that Windows serves NTP on UDP/123 and that the Pi can reach it.")


def get_command(server_ip, port, pi_name, prtt_ms=None, asym_ms=None,
                chrony=None):
    query = {"id": pi_name, "t0": f"{time.time():.6f}"}
    if prtt_ms is not None:
        query["prtt"] = f"{prtt_ms:.1f}"
    if asym_ms is not None:
        query["asym"] = f"{asym_ms:.1f}"
    if chrony:
        query.update({
            "chrony_available": int(chrony["available"]),
            "chrony_sync": int(chrony["synchronized"]),
            "chrony_leap": chrony.get("leap") or "",
            "chrony_stratum": chrony.get("stratum") or "",
            "chrony_dispersion": chrony.get("root_dispersion") or "",
            "chrony_skew": chrony.get("skew") or "",
            "chrony_offset": (chrony.get("offset")
                              if chrony.get("offset") is not None else ""),
            "chrony_source": chrony.get("source") or "",
            "chrony_error": chrony.get("error") or "",
        })
    url = (f"http://{server_ip}:{port}/get_command?" +
           urllib.parse.urlencode(query))
    with urllib.request.urlopen(url, timeout=10) as response:
        raw = response.read().decode("utf-8")
    reply = json.loads(raw)
    return reply.get("command"), chrony or {}


# ----------------- Audio checkpoint execution -----------------
def is_audio_checkpoint_command(command):
    return "play_wav.py" in command


def run_audio_checkpoint(command, target, stop_event):
    """Preload play_wav immediately, wait for READY, then send GO at target."""
    words = [os.path.expandvars(os.path.expanduser(w)) for w in shlex.split(command)]
    if not is_audio_checkpoint_command(" ".join(words)) or not is_allowed_program(words[0]):
        return None

    diagnostics = []
    launch_at = time.time()
    diagnostics.append(f"Emitter play_wav launch: {launch_at:.9f} ({(launch_at-target)*1000:+.1f} ms vs T)")

    # Report the exact file that the child has been asked to play.  This is done
    # here, before launching play_wav, so a path/packet mismatch is visible even
    # if audio-device initialisation subsequently fails.
    try:
        if "--file" in words:
            raw_path = words[words.index("--file") + 1]
            wav_path = raw_path if os.path.isabs(raw_path) else os.path.join(COMMAND_CWD or os.getcwd(), raw_path)
            wav_path = os.path.abspath(wav_path)
            diagnostics.append(f"Emitter WAV path: {wav_path}")
            diagnostics.append(f"Emitter WAV exists: {os.path.isfile(wav_path)}")
            if os.path.isfile(wav_path):
                try:
                    with wave.open(wav_path, "rb") as wf:
                        frames, rate, channels = wf.getnframes(), wf.getframerate(), wf.getnchannels()
                    diagnostics.append(
                        f"Emitter WAV metadata: {frames} frames, {rate} Hz, {channels} ch, "
                        f"{frames / rate:.3f} s")
                except (wave.Error, OSError) as error:
                    diagnostics.append(f"Emitter WAV metadata unavailable: {error}")
    except (ValueError, IndexError) as error:
        diagnostics.append(f"Emitter WAV inspection failed: {error}")

    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    env = dict(os.environ)
    env.update({"LBB_AUDIO_READY_FD": str(ready_w), "LBB_AUDIO_GO_FD": str(go_r)})
    proc = None
    try:
        proc = subprocess.Popen(words, cwd=COMMAND_CWD, env=env,
                                pass_fds=(ready_w, go_r), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=(os.name == "posix"))
        register_process(proc)
        diagnostics.append(f"Emitter play_wav PID: {proc.pid}")
        os.close(ready_w)
        os.close(go_r)
        ready_bytes = os.read(ready_r, 64)
        ready_at = time.time()
        os.close(ready_r)
        diagnostics.append(
            f"Emitter play_wav READY: {ready_at:.9f} "
            f"({(ready_at-target)*1000:+.1f} ms vs T; {(ready_at-launch_at)*1000:.1f} ms after launch; "
            f"signal={ready_bytes!r})")

        if ready_at > target:
            diagnostics.append(f"WARNING: play_wav became READY {(ready_at-target)*1000:.1f} ms after T")

        while time.time() < target:
            if stop_event.is_set():
                proc.terminate()
                return {"output": "\n".join(diagnostics + ["audio checkpoint cancelled"]),
                        "checkpoint_at": None, "trigger_error_ms": None,
                        "checkpoint_error_ms": None, "status": "cancelled"}
        trigger_at = time.time()
        os.write(go_w, b"G")
        os.close(go_w)
        diagnostics.append(f"Emitter GO sent: {trigger_at:.9f} ({(trigger_at-target)*1000:+.3f} ms vs T)")
        try:
            stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        finally:
            unregister_process(proc)
        exit_at = time.time()
        diagnostics.append(f"Emitter play_wav exit: {exit_at:.9f}; return code={proc.returncode}")
        child_text = (stdout or "") + (stderr or "")
        checkpoint = next((float(line.split(":", 1)[1]) for line in child_text.splitlines()
                           if line.startswith("AUDIO_CHECKPOINT:")), None)
        if checkpoint is not None:
            diagnostics.append(f"Emitter AUDIO_CHECKPOINT: {checkpoint:.9f} ({(checkpoint-target)*1000:+.3f} ms vs T)")
        if child_text.strip():
            diagnostics.append("play_wav output:\n" + child_text.strip())
        return {"output": "\n".join(diagnostics),
                "checkpoint_at": checkpoint,
                "trigger_error_ms": (trigger_at - target) * 1000,
                "checkpoint_error_ms": ((checkpoint - target) * 1000
                                         if checkpoint is not None else None),
                "status": "ok" if proc.returncode == 0 else "error"}
    except (OSError, subprocess.SubprocessError) as error:
        if proc is not None:
            unregister_process(proc)
        diagnostics.append(f"Emitter playback exception: {error}")
        return {"output": "\n".join(diagnostics), "checkpoint_at": None,
                "trigger_error_ms": (time.time() - target) * 1000,
                "checkpoint_error_ms": None, "status": "error"}


# ----------------- Binaural localisation execution -----------------
def handle_binaural_command(server_ip, port, pi_name, command, stop_event):
    """Launch microphone capture immediately; the listener obtains T afterwards.

    ``__binaural__`` intentionally contains no emission epoch.  The worker starts
    the microphone, reports READY, and then obtains the server-selected T through
    /binaural_trigger.  This makes a late command poll harmless: emission cannot be
    scheduled until this process is already recording.
    """
    parts = command.split(":", 2)
    if len(parts) != 3 or parts[0] != "__binaural__":
        return
    run_id, script_command = parts[1:]

    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        try:
            http_post(server_ip, port, "/post_result", {
                "id": pi_name,
                "message": f"Binaural test {run_id} not started: chrony is not synchronized",
            })
        except OSError:
            pass
        return

    words = [os.path.expandvars(os.path.expanduser(w)) for w in shlex.split(script_command)]
    if not words or "binaural_chirp_test.py" not in " ".join(words):
        return

    env = dict(os.environ)
    env.update({
        "LBB_BINAURAL_SERVER": str(server_ip),
        "LBB_BINAURAL_PORT": str(port),
        "LBB_BINAURAL_PI_NAME": str(pi_name),
        "LBB_BINAURAL_RUN_ID": str(run_id),
    })

    try:
        proc = subprocess.Popen(
            words, cwd=COMMAND_CWD, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=(os.name == "posix")
        )
        register_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        finally:
            unregister_process(proc)
        output = ((stdout or "") + (stderr or "")).strip()
        if output:
            http_post(server_ip, port, "/post_result", {
                "id": pi_name, "message": output,
            })
    except (OSError, subprocess.SubprocessError) as error:
        try:
            http_post(server_ip, port, "/post_result", {
                "id": pi_name, "message": f"Binaural test failed: {error}",
            })
        except OSError:
            pass


def handle_spatial_emit(server_ip, port, pi_name, command, stop_event):
    """Preload the emitter as soon as the already-ready listeners permit it."""
    parts = command.split(":", 4)
    if len(parts) != 5:
        return
    _, run_id, prepare_text, emit_text, script_command = parts
    prepare_at, emit_at = float(prepare_text), float(emit_text)
    received_at = time.time()
    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        report_sync_result(server_ip, port, pi_name, run_id, emit_at, None,
                           "unsynchronized", "chrony is not synchronized", chrony)
        return

    while time.time() < prepare_at:
        if stop_event.is_set():
            return
        time.sleep(min(0.01, max(0.0, prepare_at - time.time())))

    # The server only queues this emitter command after every listener has
    # reported /binaural_ready.  Ask permission immediately, rather than waiting
    # until T-200 ms, so play_wav gets the full emission lead to initialise.
    permission_request_at = time.time()
    try:
        reply = json.loads(http_post(server_ip, port, "/spatial_emit_permission", {
            "id": pi_name, "run": run_id
        }))
    except Exception as error:
        report_sync_result(server_ip, port, pi_name, run_id, emit_at, None,
                           "cancelled", f"emission permission failed: {error}", chrony)
        return
    permission_at = time.time()
    if not reply.get("ok"):
        report_sync_result(server_ip, port, pi_name, run_id, emit_at, None,
                           "cancelled", reply.get("error", "listeners not ready"), chrony)
        return

    result = run_audio_checkpoint(script_command, emit_at, stop_event)
    if result is not None:
        prefix = (
            f"Emitter command received: {received_at:.9f} ({(received_at-emit_at)*1000:+.1f} ms vs T)\n"
            f"Emitter permission requested: {permission_request_at:.9f}\n"
            f"Emitter permission granted: {permission_at:.9f} ({(permission_at-emit_at)*1000:+.1f} ms vs T)"
        )
        result["output"] = prefix + "\n" + result.get("output", "")
        report_sync_result(server_ip, port, pi_name, run_id, emit_at,
                           result.get("checkpoint_at"), result["status"],
                           result["output"], chrony, audio_result=result)


# ----------------- Assignment helpers -----------------
GENERATION_DIR = os.path.join(
    os.path.expanduser("~"), "NoBlackBoxes", "LastBlackBox", "boxes", "audio",
    "signal-processing", "python", "generation")
WAV_DIR = os.path.join(GENERATION_DIR, "wav")
DEFAULT_WAV_FILE = os.path.join(GENERATION_DIR, "default_wav.txt")
SPATIAL_SIT_OUT_EXIT_CODE = 20


def local_wavs():
    if not os.path.isdir(WAV_DIR):
        return []
    # The calibration chirp is infrastructure, never an orchestral role.
    return sorted(
        f for f in os.listdir(WAV_DIR)
        if f.lower().endswith(".wav")
        and f.casefold() != "localisation_chirp.wav"
        and os.path.isfile(os.path.join(WAV_DIR, f))
    )


def handle_spatial_inventory(server_ip, port, pi_name, command):
    parts = command.split(":", 1)
    if len(parts) != 2:
        return
    http_post(server_ip, port, "/spatial_inventory", {
        "id": pi_name, "session": parts[1], "wavs": json.dumps(local_wavs())
    })


def handle_spatial_assignment(server_ip, port, pi_name, command, stop_event):
    parts = command.split(":", 2)
    if len(parts) != 3:
        return
    session_id, encoded = parts[1], parts[2]
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode()).decode("utf-8"))
    wav = payload.get("wav")
    if wav:
        if wav not in local_wavs():
            raise RuntimeError(f"Server assigned unavailable WAV: {wav}")
        with open(DEFAULT_WAV_FILE, "w", encoding="utf-8") as f:
            f.write(wav + "\n")
        http_post(server_ip, port, "/spatial_assignment_ack", {
            "id": pi_name, "session": session_id, "wav": wav, "status": "ok"
        })
        return

    http_post(server_ip, port, "/spatial_assignment_ack", {
        "id": pi_name, "session": session_id, "wav": "", "status": "sitout"
    })
    # Match assign_wavs.py semantics: an excess Pi leaves the message server.
    stop_event.set()
    os._exit(SPATIAL_SIT_OUT_EXIT_CODE)


def handle_assign_command(server_ip, port, pi_name, command, stop_event):
    """Restore the existing assign_wavs.py synchronized path."""
    parts = command.split(":", 4)
    if len(parts) != 5:
        return
    _, run_id, stamp_text, session_id, script_command = parts
    target = float(stamp_text)
    while time.time() < target:
        if stop_event.is_set():
            return
        time.sleep(min(0.01, max(0.0, target-time.time())))
    words = [os.path.expandvars(os.path.expanduser(w)) for w in shlex.split(script_command)]
    words += ["--server", server_ip, "--port", str(port), "--name", pi_name,
              "--session", session_id]
    proc = subprocess.run(words, cwd=COMMAND_CWD, capture_output=True, text=True,
                          timeout=COMMAND_TIMEOUT)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    chrony = read_chrony_status()
    report_sync_result(server_ip, port, pi_name, run_id, target, target,
                       "ok" if proc.returncode in (0, 20) else "error", output, chrony)
    if output:
        http_post(server_ip, port, "/post_result", {"id": pi_name, "message": output})
    if proc.returncode == 20:
        stop_event.set()
        os._exit(20)


# ----------------- Scheduled runs: run at an EXACT server-clock time --------
def get_board(server_ip, port, since):
    # Returns every public board entry AFTER number "since" (new ones only)
    url = f"http://{server_ip}:{port}/get_board?since={since}"
    with urllib.request.urlopen(url, timeout=10) as response:
        reply = json.loads(response.read().decode("utf-8"))
    return reply.get("entries", [])

# ----------------- Helper: run one command (safely!) -----------------
def run_command(command):
    """Run one approved command in the Pi client's persistent working directory."""
    global COMMAND_CWD
    # Split "vcgencmd measure_temp" into ["vcgencmd", "measure_temp"]
    words = shlex.split(command)
    words = [os.path.expandvars(os.path.expanduser(word)) for word in words]
    if not words:
        return "(empty command)"
    if not is_allowed_program(words[0]):
        return f"(command not allowed: {words[0]!r} — ask the laptop's owner to whitelist it)"
    if words[0] == "cd":
        if len(words) > 2:
            return "(cd accepts one directory)"
        target = words[1] if len(words) == 2 else os.path.expanduser("~")
        if not os.path.isabs(target):
            target = os.path.join(COMMAND_CWD or os.getcwd(), target)
        target = os.path.abspath(os.path.expanduser(target))
        if not os.path.isdir(target):
            return f"(cd failed: directory not found: {target})"
        COMMAND_CWD = target
        return f"(working directory changed to {target})"
    # The command is already tokenized and is executed without a shell, so
    # shell operators such as ;, &&, |, and redirection cannot be interpreted.
    try:
        # text=True gives us a str instead of bytes; capture both output and errors.
        if words[0] == "source":
            # Supported form: source /path/to/activate && python script.py
            # Keep this deliberately narrow: no arbitrary chained shell syntax.
            if "&&" not in words:
                if len(words) != 2:
                    return "(source accepts one activation script, or source PATH && python SCRIPT)"
                activation, command_words = words[1], None
            else:
                split_at = words.index("&&")
                if words.count("&&") != 1 or split_at < 1 or split_at + 1 >= len(words):
                    return "(source supports only: source PATH && python SCRIPT)"
                activation = words[1]
                command_words = words[split_at + 1:]
                if (len(command_words) < 2 or
                        not is_allowed_program(command_words[0]) or
                        re.split(r"[/\\\\]", command_words[0])[-1].lower() not in
                        ("python", "python3") and
                        not PYTHON_EXECUTABLE_RE.fullmatch(
                            re.split(r"[/\\\\]", command_words[0])[-1])):
                    return "(source chaining supports only: source PATH && python SCRIPT)"
            if not os.path.isabs(activation):
                activation = os.path.join(COMMAND_CWD or os.getcwd(), activation)
            activation = os.path.abspath(os.path.expanduser(activation))
            if not os.path.isfile(activation):
                return f"(source failed: activation script not found: {activation})"
            if command_words is None:
                invocation = ["bash", "-c", f"source {shlex.quote(activation)}"]
            else:
                # shell-quote every token so this is one controlled shell
                # pipeline, not a general-purpose command parser.
                command_text = shlex.join(command_words)
                invocation = [
                    "bash", "-c",
                    f"source {shlex.quote(activation)} && {command_text}",
                ]
        else:
            invocation = ["bash", "-ic", "Activate"] if words[0] == "Activate" else words
        process = subprocess.Popen(
            invocation, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=COMMAND_CWD,
            start_new_session=(os.name == "posix")
        )
        register_process(process)
        try:
            stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT)
        finally:
            unregister_process(process)
        text = (stdout or "") + (stderr or "")
        if process.returncode != 0:
            text = (f"(exit status {process.returncode})\n" + text).strip()
        return text.strip() or "(command produced no output)"
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

        details = stdout + stderr
        suffix = f"\n{details.strip()}" if details.strip() else ""
        return f"(command timed out after {COMMAND_TIMEOUT}s){suffix}"
    except OSError as error:
        if words[0] == "sudo" and "password" in str(error).lower():
            return ("(sudo needs authentication; configure passwordless sudo for "
                    "the Pi client user, or run the client as root)")
        return f"(failed to run: {error})"

# ----------------- Benchmark: measure initiation, Python startup, script ----------
def run_benchmark_once(command, intended_at, executed_at, stop_event):
    """Run one Python script and return absolute UTC timing samples."""
    words = shlex.split(command)
    words = [os.path.expandvars(os.path.expanduser(word)) for word in words]
    if len(words) < 2 or not is_allowed_program(words[0]) or not words[1].endswith(".py"):
        return {"status": "rejected", "output": "benchmark requires: python SCRIPT.py",
                "initiation_jitter_ms": None, "python_startup_ms": None,
                "script_execution_ms": None, "execution_error_ms": None}
    read_fd, write_fd = os.pipe()
    wrapper = (f"import os,runpy,sys;os.write({write_fd},b'READY\\n');"
               "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')")
    started = executed_at
    try:
        proc = subprocess.Popen([words[0], "-c", wrapper] + words[1:],
                                cwd=COMMAND_CWD, pass_fds=(write_fd,),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True,
                                start_new_session=(os.name == "posix"))
        register_process(proc)
        os.close(write_fd)
        os.read(read_fd, 5)
        ready = time.time()
        os.close(read_fd)
        try:
            stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        finally:
            unregister_process(proc)
        finished = time.time()
        output = (stdout or "") + (stderr or "")
        return {"status": "ok" if proc.returncode == 0 else "error",
                "output": output.strip() or "(command produced no output)",
                "initiation_jitter_ms": (started - intended_at) * 1000,
                "execution_error_ms": (started - intended_at) * 1000,
                "python_startup_ms": (ready - started) * 1000,
                "script_execution_ms": (finished - ready) * 1000}
    except (OSError, subprocess.SubprocessError) as error:
        for fd in (read_fd, write_fd):
            try: os.close(fd)
            except OSError: pass
        return {"status": "error", "output": str(error),
                "initiation_jitter_ms": (started - intended_at) * 1000,
                "execution_error_ms": (started - intended_at) * 1000,
                "python_startup_ms": None, "script_execution_ms": None}


# ----------------- Benchmark result reporting -----------------
def report_benchmark_result(server_ip, port, pi_name, session_id, iteration,
                            intended_at, result, chrony):
    fields = {"id": pi_name, "session": session_id, "iteration": iteration,
              "intended_at": f"{intended_at:.6f}", "status": result.get("status", "error"),
              "output": result.get("output", "")[:500],
              "initiation_jitter_ms": result.get("initiation_jitter_ms"),
              "execution_error_ms": result.get("execution_error_ms"),
              "python_startup_ms": result.get("python_startup_ms"),
              "script_execution_ms": result.get("script_execution_ms"),
              "chrony_sync": int(chrony.get("synchronized", False))}
    try:
        http_post(server_ip, port, "/benchmark_result", fields)
    except OSError:
        pass


def report_sync_result(server_ip, port, pi_name, run_id, intended_at, executed_at,
                       status, output, chrony=None, audio_result=None):
    """Report the intended and actual UTC epoch instants to the server."""
    fields = {"id": pi_name, "run": run_id, "status": status, "output": output,
              "intended_at": f"{intended_at:.6f}",
              "report_sent_at": f"{time.time():.6f}"}
    if executed_at is not None:
        fields["executed_at"] = f"{executed_at:.6f}"
    if audio_result:
        fields.update({
            "audio_trigger_error_ms": audio_result.get("trigger_error_ms"),
            "audio_checkpoint_error_ms": audio_result.get("checkpoint_error_ms"),
            "audio_checkpoint_at": audio_result.get("checkpoint_at"),
        })

    if chrony:
        fields.update({
            "chrony_sync": int(chrony["synchronized"]),
            "chrony_leap": chrony.get("leap") or "",
            "chrony_stratum": chrony.get("stratum") or "",
            "chrony_dispersion": chrony.get("root_dispersion") or "",
            "chrony_skew": chrony.get("skew") or "",
            "chrony_offset": (chrony.get("offset")
                              if chrony.get("offset") is not None else ""),
            "chrony_source": chrony.get("source") or "",
        })
    try:
        http_post(server_ip, port, "/sync_result", fields)
    except OSError:
        pass


def handle_benchmark_command(server_ip, port, pi_name, command, stop_event):
    parts = command.split(":", 6)
    if len(parts) != 7 or parts[0] != "__bench__":
        return
    _, session_id, iteration_text, repetitions_text, gap_text, stamp_text, script_command = parts
    try:
        iteration, repetitions = int(iteration_text), int(repetitions_text)
        gap_s, intended_at = float(gap_text), float(stamp_text)
    except ValueError:
        return
    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        result = {"status": "unsynchronized", "output": "chrony is not synchronized"}
        report_benchmark_result(server_ip, port, pi_name, session_id, iteration,
                                intended_at, result, chrony)
        return
    while time.time() < intended_at:
        if stop_event.is_set(): return
        pass
    executed_at = time.time()
    result = run_benchmark_once(script_command, intended_at, executed_at, stop_event)
    report_benchmark_result(server_ip, port, pi_name, session_id, iteration,
                            intended_at, result, chrony)
    if iteration + 1 < repetitions:
        try:
            # The server queues the next iteration using the same benchmark session.
            http_post(server_ip, port, "/benchmark_next", {
                "id": pi_name, "session": session_id,
                "iteration": iteration + 1, "repetitions": repetitions,
                "gap_s": gap_s, "command": script_command,
            })
        except OSError:
            pass


def handle_audio_command(server_ip, port, pi_name, command, stop_event):
    parts = command.split(":", 4)
    if len(parts) != 5 or parts[0] != "__audio__":
        return
    run_id, prepare_text, playback_text, script_command = parts[1:]
    prepare_at, playback_at = float(prepare_text), float(playback_text)
    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        report_sync_result(server_ip, port, pi_name, run_id, playback_at, None,
                           "unsynchronized", "chrony is not synchronized", chrony)
        return
    while time.time() < prepare_at:
        if stop_event.is_set(): return
        pass
    result = run_audio_checkpoint(script_command, playback_at, stop_event)
    if result is not None:
        report_sync_result(server_ip, port, pi_name, run_id, playback_at,
                           result.get("checkpoint_at"), result["status"],
                           result["output"], chrony, audio_result=result)
        try:
            http_post(server_ip, port, "/post_result", {"id": pi_name, "message": result["output"]})
        except OSError:
            pass


def handle_sync_run(server_ip, port, pi_name, command, stop_event):
    """Run at the server's UTC instant, measured by the chrony-disciplined Pi."""
    parts = command.split(":", 3)
    if len(parts) < 4:
        return
    run_id, stamp_text, shell_command = parts[1], parts[2], parts[3]
    try:
        target = float(stamp_text)
    except ValueError:
        return
    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        report_sync_result(server_ip, port, pi_name, run_id, target, None,
                           "unsynchronized", "chrony is not synchronized", chrony)
        return
    if target <= time.time():
        report_sync_result(server_ip, port, pi_name, run_id, target, None,
                           "missed", "picked up after intended time", chrony)
        return
    coarse = target - time.time() - SYNC_SPIN_MARGIN
    if coarse > 0:
        stop_event.wait(coarse)
        if stop_event.is_set():
            return
    # time.time() follows the UTC wall clock disciplined by chronyd. A clock
    # step before T is captured as execution error rather than hidden.
    while time.time() < target:
        pass
    # Audio scripts are prepared early and triggered at the intended instant.
    audio_result = run_audio_checkpoint(shell_command, target, stop_event)
    if audio_result is not None:
        result = audio_result["output"]
        report_sync_result(
            server_ip, port, pi_name, run_id, target,
            audio_result.get("checkpoint_at"), audio_result["status"], result, chrony,
            audio_result=audio_result,
        )
        try:
            http_post(server_ip, port, "/post_result", {"id": pi_name, "message": result})
        except OSError:
            pass
        return

   # Capture the execution timestamp immediately before starting the command.
    # This is the Pi's best estimate of the actual command start time.
    executed_at = time.time()

    # The server receives the timing report; synchronization telemetry is
    # intentionally kept off the Pi terminal.
    result = run_command(shell_command)

    # Report the already-recorded execution time afterwards.
    # The HTTP reporting delay therefore does NOT affect executed_at.
    report_sync_result(
        server_ip, port, pi_name, run_id, target, executed_at,
        "ok", result, chrony
    )
    # Keep command output/errors in the private command feed, separate from
    # the timing-only synchronized-run panel.
    try:
        http_post(server_ip, port, "/post_result", {
            "id": pi_name,
            "message": result,
        })
    except OSError:
        pass

# ----------------- Background thread: commands + board mirror -----------------
def poller(server_ip, port, pi_name, stop_event):
    """Runs in its own thread, forever: fetches commands meant for this Pi
    (running them privately) AND relays the addressed public board messages.

    ADDRESSING (keeps the Pi's display clean):
    - A board message is shown here ONLY if it starts with "@All" (for
      everyone) or "@<this-pi's-name>" (for us) -- matched case-insensitively
      as the very first word, so "@Allison ..." does NOT match "@All".
    - Command traffic is NEVER shown: results are not on the board at all,
      and the received/executed chatter stays in the laptop's terminal only
      (hence the poller runs quietly: no print for commands/results).
    - Internal protocol commands are intercepted BEFORE the whitelist, including
      "__ping__", scheduled-run tokens, and "__mod_disconnect__". None of these
      control tokens is ever passed to a shell.
    - Our own messages are skipped (we saw them when we typed them)."""
    own_tag = f"🤖 {pi_name}".lower()

    def addressed_to_us(text):
        # True if text starts with "@All" or "@<pi-name>" as a whole word
        first_word = text.split(None, 1)[0] if text.split() else ""
        first_word = first_word.rstrip(",:").lower()
        return first_word in ("@all", f"@{pi_name.lower()}")

    last_seen = 0  # How many public board entries we have already checked
    last_prtt = None  # Pi-measured round trip (ms), reported on the NEXT poll
    while not stop_event.is_set():  # Keep polling until told to stop
        try:
            chrony = read_chrony_status()
            tick = time.perf_counter()
            command, chrony = get_command(
                server_ip, port, pi_name, last_prtt, chrony=chrony)
            last_prtt = (time.perf_counter() - tick) * 1000
            if command and command.startswith("__mod_disconnect__"):
                # Moderator control token: protocol-only, intercepted before the
                # whitelist and NEVER passed to a shell or subprocess.
                reason = command.split(":", 1)[1] if ":" in command else "moderator"
                print(f"⛔ Disconnected by Moderator ({reason.replace('_', ' ')}).", flush=True)
                stop_event.set()
                os._exit(23)
            elif command and command.startswith("__audio__"):
                handle_audio_command(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__spatial_emit__"):
                handle_spatial_emit(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__binaural__"):
                handle_binaural_command(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__spatial_inventory__"):
                handle_spatial_inventory(server_ip, port, pi_name, command)
            elif command and command.startswith("__spatial_assignment__"):
                handle_spatial_assignment(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__assign__"):
                handle_assign_command(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__bench__"):
                handle_benchmark_command(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__at__"):
                # SCHEDULED RUN: fires at a server-clock instant, reports the
                # deviation back. Never reaches the shell as-is.
                handle_sync_run(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__ping__"):
                # LAG PROBE, not a shell command: answer POST /pong at once
                # (no whitelist, no subprocess, no output) so the SERVER can
                # time server->Pi->server. Silent: stays off the Pi's display
                # and off the command feed.
                try:
                    http_post(server_ip, port, "/pong", {"id": pi_name})
                except OSError:
                    pass  # Next ping heals it; never break the loop over this
            elif command:
                # Quiet: command previews/results belong on the laptop, not
                # on this Pi's display (the server prints them in ITS
                # terminal and its private command feed).
                result = run_command(command)
                # Command results are PRIVATE: only the server's terminal and
                # command feed ever see them
                http_post(server_ip, port, "/post_result",
                          {"id": pi_name, "message": result})
            # Show addressed public board messages we have not seen yet
            for entry in get_board(server_ip, port, last_seen):
                last_seen = entry["n"]
                # Skip our own messages (we already saw them when we sent them)
                if entry["sender"].lower() == own_tag:
                    continue
                if addressed_to_us(entry["text"]):
                    print(f"💬 {entry['sender']}: {entry['text']}", flush=True)
        except OSError as error:
            print(f"❌ Lost contact with the server: {error}", flush=True)
            return  # End this thread (the main program keeps running)
        # Sleep -- but wake up IMMEDIATELY if the main thread asks us to stop
        stop_event.wait(POLL_SECONDS)

# ----------------- Main program -----------------
if __name__ == "__main__":
    # Read settings from the command line
    args = sys.argv[1:]
    setup_mode = "--setup-chrony" in args
    if setup_mode:
        args.remove("--setup-chrony")
    # Optional "--name mypi" flag: otherwise the Pi's hostname is its name
    pi_name = socket.gethostname()
    if "--name" in args:
        index = args.index("--name")
        pi_name = args[index + 1]
        del args[index:index + 2]
    server_ip = resolve_host(args[0]) if args else "127.0.0.1"
    port = int(args[1]) if len(args) > 1 and args[1].isdigit() else 8000

    if setup_mode:
        try:
            setup_chrony(server_ip)
        except (PermissionError, FileNotFoundError, RuntimeError) as error:
            print(f"❌ Chrony setup failed: {error}", flush=True)
            sys.exit(1)
        sys.exit(0)

    # Optional: an intro message (after the port), otherwise say hello
    intro = " ".join(args[2:]) if len(args) > 2 else "Hello chat!"

    print(f"🤖 [{pi_name}] Connecting to server {server_ip}:{port} ...", flush=True)
    try:
        # "id" tells the server who we are; the message goes on the PUBLIC board
        http_post(server_ip, port, "/", {"id": pi_name, "message": intro})
        print(f"✅ Intro message sent: {intro!r}", flush=True)
    except OSError as error:
        print(f"❌ Could not reach the server: {error}", flush=True)
        print("   - Is the server script running on the laptop?", flush=True)
        print("   - Are both devices on the same network?", flush=True)
        print(f"   - Is {server_ip} the laptop's correct IP address?", flush=True)
        sys.exit(1)

    # Kill All uses an independent Pi-side listener on port+1. Keep this
    # separate from the command poller so it remains responsive while a child
    # command is running.
    control_listener = threading.Thread(
        target=serve_control_port, args=(port,), daemon=True
    )
    control_listener.start()

    # A flag the main thread flips when it wants the poller to stop
    stop_event = threading.Event()
    # Start the command/board poller in a background thread. "daemon=True"
    # means it can never keep the program alive by itself
    listener = threading.Thread(
        target=poller, args=(server_ip, port, pi_name, stop_event), daemon=True
    )
    listener.start()

    # THIS thread now handles YOUR typing: every line you enter is posted
    # straight to the public message board on the website
    print(f"💬 Type a message and press Enter to post it on the website", flush=True)
    print("   (the server can also schedule a command here at an exact "
          "instant; see 'Run in sync' on the website)", flush=True)
    print(f"   (type 'quit' or press Control + C to exit)\n", flush=True)
    try:
        while True:
            text = input()  # Waits for you to type a line + press Enter
            if text.strip().lower() == "quit":
                print("Shutting down...", flush=True)
                break
            if not text.strip():
                continue    # Ignore empty lines
            try:
                # "id" tells the server who we are; goes on the PUBLIC board
                http_post(server_ip, port, "/", {"id": pi_name, "message": text})
                print(f"✅ Posted: {text!r}", flush=True)
            except OSError as error:
                print(f"❌ Could not send message: {error}", flush=True)
    except (KeyboardInterrupt, EOFError):
        # Control + C, or input() ending because stdin closed (e.g. a pipe)
        print("\nShutting down...", flush=True)
    # Ask the poller to stop, then WAIT for it to exit. This matters because
    # it may be halfway through posting a command's result -- without this
    # wait, quitting could kill the result before it reaches the server.
    # The timeout is only a safety net: normally the poller stops within
    # POLL_SECONDS, or right after posting any in-flight result
    stop_event.set()
    listener.join(timeout=COMMAND_TIMEOUT + POLL_SECONDS + 2)
    #FIN

