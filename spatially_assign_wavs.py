#!/usr/bin/env python3
"""Spatial orchestral part assignment for a recovered robot layout.

This module does not localise robots; itd_solver.py does that.  It takes the
scale-free 2-D positions and the WAV filenames available to the orchestra and
matches stems to seats using a conventional orchestral-family layout.

The mapping is intentionally semantic rather than a fixed list of filenames:
individual instruments (Violin, Trumpet, Flute, ...), section names (Strings,
Brass, Woodwinds, Percussion, Choir), and combined stems are all recognised.
For a combined stem the preferred seat is the average of the recognised
families. Unknown/generic MIDI roles are distributed over the stage instead of
causing an error.

Important: ITD-only localisation has an unavoidable global reflection
ambiguity.  We canonicalise the recovered map deterministically for stable
assignment, but 'left' and 'right' are relative orchestral-map directions, not
an externally measured physical stage-left/stage-right reference.
"""
import argparse
import json
import os
import re
import numpy as np

try:
    from scipy.optimize import linear_sum_assignment
except ImportError as exc:
    raise SystemExit("spatially_assign_wavs.py requires scipy") from exc

# Preferred (x, y) seats in a normalized stage coordinate system.
# x=-1 is map-left, x=+1 map-right; y=-1 is front, y=+1 is rear.
FAMILY_SEATS = {
    "strings": (-0.65, -0.55),
    "woodwinds": (-0.05, 0.00),
    "brass": (0.55, 0.35),
    "percussion": (0.70, 0.80),
    "choir": (0.00, 0.85),
    "keyboard_harp": (-0.35, 0.20),
    "bass": (0.15, 0.55),
    "guitar": (-0.25, 0.15),
    "voice": (0.00, 0.65),
}

ALIASES = {
    "strings": ["string", "violin", "viola", "cello", "violoncello", "fiddle"],
    "bass": ["contrabass", "double bass", "upright bass", "bass"],
    "woodwinds": ["woodwind", "flute", "piccolo", "oboe", "clarinet", "bassoon", "sax", "recorder"],
    "brass": ["brass", "trumpet", "trombone", "horn", "tuba", "cornet", "euphonium"],
    "percussion": ["percussion", "drum", "timpani", "cymbal", "snare", "marimba", "xylophone", "glockenspiel"],
    "choir": ["choir", "chorus", "choral"],
    "voice": ["vocal", "voice", "soprano", "alto", "tenor", "baritone"],
    "keyboard_harp": ["piano", "keyboard", "organ", "harp", "celesta", "harpsichord"],
    "guitar": ["guitar", "mandolin", "banjo", "ukulele"],
}


def role_name(filename):
    return os.path.splitext(os.path.basename(filename))[0]


def classify_role(filename):
    text = re.sub(r"[_\-]+", " ", role_name(filename).lower())
    hits = []
    # Bass must be checked before generic strings; double-bass stems often
    # contain both words and should sit with the low/rear section.
    for family, words in ALIASES.items():
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", text) for w in words):
            hits.append(family)
    if "bass" in hits and "strings" in hits:
        hits.remove("strings")
    return hits


def canonicalize_positions(positions, names=None):
    """Center, rotate to principal axes, normalize, and choose stable signs."""
    p = np.asarray(positions, dtype=float)
    if len(p) <= 1:
        return np.zeros_like(p)
    x = p - p.mean(axis=0)
    _, _, vt = np.linalg.svd(x, full_matrices=False)
    z = x @ vt.T
    # Longest spread becomes horizontal.
    if np.ptp(z[:, 1]) > np.ptp(z[:, 0]):
        z = z[:, [1, 0]]
    names = list(names or [str(i) for i in range(len(z))])
    order = np.argsort(np.asarray(names, dtype=str))
    # Resolve the otherwise arbitrary PCA/reflection signs deterministically.
    for axis in (0, 1):
        for k in order:
            if abs(z[k, axis]) > 1e-9:
                if z[k, axis] > 0:
                    z[:, axis] *= -1
                break
    scale = np.max(np.abs(z), axis=0)
    scale[scale < 1e-9] = 1.0
    return z / scale


def preferred_seat(filename, unknown_index=0, unknown_count=1):
    families = classify_role(filename)
    if families:
        pts = np.asarray([FAMILY_SEATS[f] for f in families], dtype=float)
        return pts.mean(axis=0), families
    # Generic MIDI/program names: spread them across the usable stage so an
    # unfamiliar filename never prevents assignment.
    frac = (unknown_index + 0.5) / max(1, unknown_count)
    return np.array([-0.85 + 1.70*frac, 0.10]), []


def assign_wavs_spatially(names, positions, wavs):
    """Return (assignments, unused_wavs, sitout_pis, canonical_positions)."""
    names = list(names)
    wavs = sorted(dict.fromkeys(wavs), key=str.casefold)
    p = canonicalize_positions(positions, names)
    if not names or not wavs:
        return {}, wavs, names, p.tolist(), {}

    # At most one part per Pi and one Pi per part.
    active_count = min(len(names), len(wavs))

    unknown_total = sum(not classify_role(w) for w in wavs)
    unknown_i = 0
    seats = []
    metadata = []
    for wav in wavs:
        seat, families = preferred_seat(wav, unknown_i, unknown_total)
        if not families:
            unknown_i += 1
        seats.append(seat)
        metadata.append(families)
    seats = np.asarray(seats)

    # Squared spatial distance is the base cost.  A mild radial/depth term
    # discourages every unknown/combined stem from collapsing to the same row.
    cost = np.sum((p[:, None, :] - seats[None, :, :])**2, axis=2)
    rows, cols = linear_sum_assignment(cost)

    # linear_sum_assignment returns min(Npis,Nwavs) matches automatically.
    assignments = {names[int(r)]: wavs[int(c)] for r, c in zip(rows, cols)}
    assigned_wavs = set(assignments.values())
    unused = [w for w in wavs if w not in assigned_wavs]
    sitout = [n for n in names if n not in assignments]

    detail = {
        wav: {"families": metadata[i], "preferred_seat": seats[i].tolist()}
        for i, wav in enumerate(wavs)
    }
    return assignments, unused, sitout, p.tolist(), detail


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True,
                    help="JSON containing names, positions and wavs")
    ap.add_argument("--output")
    ap.add_argument(
        "-v", "--volume", type=float, default=100.0,
        help=("Localisation-chirp volume (0-100) when this command is used "
              "through the message-board server. Stored in standalone JSON "
              "output for reproducibility."),
    )
    args = ap.parse_args()
    if not 0 <= args.volume <= 100:
        ap.error("volume must be between 0 and 100")
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    result = assign_wavs_spatially(data["names"], data["positions"], data["wavs"])
    assignments, unused, sitout, canonical, detail = result
    payload = {"assignments": assignments, "unused_wavs": unused,
               "sitout_pis": sitout, "canonical_positions": canonical,
               "role_metadata": detail, "volume": args.volume}
    text = json.dumps(payload, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
