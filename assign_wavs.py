#!/usr/bin/env python3
"""Distributed WAV-role negotiation for the No Black Boxes orchestra.

The server controls session membership, turn order and conflict-free assignment.
Each Pi generates and keeps its own random preference scores locally.
"""
import argparse
import json
import os
import random
import socket
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WAV_DIR = os.path.join(HERE, "wav")
DEFAULT_WAV_FILE = os.path.join(HERE, "default_wav.txt")
POLL_SECONDS = 0.25
HTTP_TIMEOUT = 10
SIT_OUT_EXIT_CODE = 20  # tells pi_message_client.py to disconnect completely


def post(server, port, path, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        f"http://{server}:{port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {"ok": True}


def get_state(server, port, name, session):
    query = urllib.parse.urlencode({"id": name, "session": session})
    with urllib.request.urlopen(
            f"http://{server}:{port}/assign_state?{query}",
            timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))["session"]


def available_wavs():
    if not os.path.isdir(WAV_DIR):
        raise RuntimeError(f"WAV directory not found: {WAV_DIR}")
    wavs = sorted(name for name in os.listdir(WAV_DIR)
                  if name.lower().endswith(".wav") and
                  os.path.isfile(os.path.join(WAV_DIR, name)))
    if not wavs:
        raise RuntimeError(f"No .wav files found in: {WAV_DIR}")
    return wavs


def choose_favourite(preferences, available):
    # Filename is the deterministic tie-breaker for equal random scores.
    return max(available, key=lambda wav: (preferences[wav], wav))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--name", default=socket.gethostname())
    parser.add_argument("--session", required=True)
    parser.add_argument("--seed", type=int, default=None,
                        help="Optional deterministic seed for testing")
    args = parser.parse_args()

    wavs = available_wavs()
    rng = random.Random(args.seed)
    preferences = {wav: rng.randint(0, 100) for wav in wavs}

    print(f"Assignment session: {args.session}")
    print("Private preferences (higher = preferred):")
    for wav, score in sorted(preferences.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {wav}: {score}")

    reply = post(args.server, args.port, "/assign_register", {
        "id": args.name, "session": args.session, "wavs": json.dumps(wavs)
    })
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "registration failed"))

    last_action = None
    while True:
        state = get_state(args.server, args.port, args.name, args.session)
        phase = state["phase"]

        if state["finished"]:
            assignment = state.get("my_assignment")
            if assignment:
                if assignment not in wavs:
                    raise RuntimeError(f"Server assigned unavailable WAV: {assignment}")
                with open(DEFAULT_WAV_FILE, "w", encoding="utf-8") as f:
                    f.write(assignment + "\n")
                print(f"Assigned: {assignment}")
                print(f"Updated: {DEFAULT_WAV_FILE}")
            else:
                try:
                    post(args.server, args.port, "/assign_sitout", {
                        "id": args.name, "session": args.session})
                except Exception:
                    pass
                print("Bye chat!")
                print("No WAV assigned; disconnecting from the message server.")
                raise SystemExit(SIT_OUT_EXIT_CODE)
            return

        available = state.get("available_wavs", [])
        speaker = state.get("speaker")

        if phase == "propose" and speaker == args.name and available:
            action = ("propose", tuple(available))
            if action != last_action:
                favourite = choose_favourite(preferences, available)
                post(args.server, args.port, "/assign_propose", {
                    "id": args.name, "session": args.session, "wav": favourite})
                last_action = action

        elif phase == "respond" and args.name != speaker and not state.get("my_assignment"):
            if args.name not in state.get("responded", []):
                proposal = state.get("proposal")
                favourite = choose_favourite(preferences, available) if available else None
                interested = proposal == favourite
                post(args.server, args.port, "/assign_response", {
                    "id": args.name, "session": args.session,
                    "interested": "1" if interested else "0"})
                last_action = ("respond", proposal)

        elif phase == "confirm" and speaker == args.name:
            action = ("confirm", state.get("proposal"))
            if action != last_action:
                post(args.server, args.port, "/assign_confirm", {
                    "id": args.name, "session": args.session})
                last_action = action

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Assignment failed: {error}")
        raise SystemExit(1)
