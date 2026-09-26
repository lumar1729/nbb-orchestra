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

import os
import shutil

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
    """Start play_wav early, wait for device readiness, trigger at target."""
    words = [os.path.expandvars(os.path.expanduser(w)) for w in shlex.split(command)]
    if not is_audio_checkpoint_command(" ".join(words)) or not is_allowed_program(words[0]):
        return None
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    env = dict(os.environ)
    env.update({"LBB_AUDIO_READY_FD": str(ready_w), "LBB_AUDIO_GO_FD": str(go_r)})
    try:
        proc = subprocess.Popen(words, cwd=COMMAND_CWD, env=env,
                                pass_fds=(ready_w, go_r), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        os.close(ready_w)
        os.close(go_r)
        os.read(ready_r, 6)
        os.close(ready_r)
        while time.time() < target:
            if stop_event.is_set():
                proc.terminate()
                return "(audio checkpoint cancelled)"
        trigger_at = time.time()
        os.write(go_w, b"G")
        os.close(go_w)
        stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        text = (stdout or "") + (stderr or "")
        checkpoint = next((float(line.split(":", 1)[1]) for line in text.splitlines()
                           if line.startswith("AUDIO_CHECKPOINT:")), None)
        return {"output": text.strip() or "(audio produced no output)",
                "checkpoint_at": checkpoint,
                "trigger_error_ms": (trigger_at - target) * 1000,
                "checkpoint_error_ms": ((checkpoint - target) * 1000
                                         if checkpoint is not None else None),
                "status": "ok" if proc.returncode == 0 else "error"}
    except (OSError, subprocess.SubprocessError) as error:
        return {"output": str(error), "checkpoint_at": None,
                "trigger_error_ms": (time.time() - target) * 1000,
                "checkpoint_error_ms": None, "status": "error"}


# ----------------- Binaural localisation execution -----------------
def handle_binaural_command(server_ip, port, pi_name, command, stop_event):
    """
    Launch the microphone worker during the preparation phase.

    The worker starts capture immediately, POSTs /binaural_ready itself, remains
    alive across the server's scheduled emission epoch, then posts its result.
    """
    parts = command.split(":", 4)
    if len(parts) != 5 or parts[0] != "__binaural__":
        return

    run_id, prepare_text, emit_text, script_command = parts[1:]
    prepare_at, emit_at = float(prepare_text), float(emit_text)

    chrony = read_chrony_status()
    if not chrony["synchronized"]:
        report_sync_result(
            server_ip, port, pi_name, run_id, emit_at, None,
            "unsynchronized", "chrony is not synchronized", chrony
        )
        return

    while time.time() < prepare_at:
        if stop_event.is_set():
            return
        time.sleep(min(0.01, max(0.0, prepare_at - time.time())))

    words = [os.path.expandvars(os.path.expanduser(w)) for w in shlex.split(script_command)]
    if not words or "binaural_chirp_test.py" not in " ".join(words):
        return

    env = dict(os.environ)
    env.update({
        "LBB_BINAURAL_SERVER": str(server_ip),
        "LBB_BINAURAL_PORT": str(port),
        "LBB_BINAURAL_PI_NAME": str(pi_name),
        "LBB_BINAURAL_RUN_ID": str(run_id),
        "LBB_BINAURAL_EMIT_AT": f"{emit_at:.9f}",
    })

    try:
        proc = subprocess.Popen(
            words, cwd=COMMAND_CWD, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        output = ((stdout or "") + (stderr or "")).strip()
        if output:
            http_post(server_ip, port, "/post_result", {
                "id": pi_name,
                "message": output,
            })
    except (OSError, subprocess.SubprocessError) as error:
        try:
            http_post(server_ip, port, "/post_result", {
                "id": pi_name,
                "message": f"Binaural test failed: {error}",
            })
        except OSError:
            pass


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
        output = subprocess.run(
            invocation, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
            cwd=COMMAND_CWD
        )
        text = output.stdout + output.stderr
        if output.returncode != 0:
            text = (f"(exit status {output.returncode})\n" + text).strip()
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
                                text=True)
        os.close(write_fd)
        os.read(read_fd, 5)
        ready = time.time()
        os.close(read_fd)
        stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
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
    - Two magic commands are intercepted BEFORE the whitelist: "__ping__"
      (lag probe -> instant POST /pong) and "__at__:<run>:<T>:<cmd>" (a
      scheduled run -> wait for T on the server's clock, run, report back).
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
            if command and command.startswith("__audio__"):
                handle_audio_command(server_ip, port, pi_name, command, stop_event)
            elif command and command.startswith("__binaural__"):
                handle_binaural_command(server_ip, port, pi_name, command, stop_event)
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

