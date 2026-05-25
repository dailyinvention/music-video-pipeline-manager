#!/usr/bin/env python3
"""
Fractal Music Video – Transition Point Calculator

Extracts every transition event that music_video.py would act on during
rendering, expressed as frame numbers.  No video is generated.

Transition types reported
─────────────────────────
  julia_section  – Julia c-value preset changes to the next preset
  palette        – Colour palette index increments to the next theme
  beat           – Beat grid hit detected by librosa
  onset          – Strong rhythmic onset / transient above threshold

Usage
─────
  python transitions.py --audio music.mp3
  python transitions.py --audio music.mp3 --fps 24 --sections 6
  python transitions.py --audio music.mp3 --onset-threshold 0.5 --csv out.csv
"""

import argparse
import os
import sys

import numpy as np
import librosa


# Number of Julia presets available (mirrors music_video.py)
NUM_JULIA_PRESETS = 16
# Number of colour palettes available (mirrors music_video.py)
NUM_PALETTES = 5


# ---------------------------------------------------------------------------
# Audio analysis  (mirrors music_video.py – same hop, same smoothing)
# ---------------------------------------------------------------------------

def analyze_audio(audio_path: str):
    print(f"Analyzing audio: {audio_path}")
    y, sr = librosa.load(audio_path, sr=None)
    duration = librosa.get_duration(y=y, sr=sr)

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    tempo_val  = float(np.atleast_1d(tempo)[0])
    print(f"  Duration: {duration:.2f}s  |  Tempo: {tempo_val:.1f} BPM  "
          f"|  Beats: {len(beat_times)}")

    hop       = 512
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    n         = len(onset_env)
    times     = librosa.frames_to_time(np.arange(n), sr=sr, hop_length=hop)

    # Same smoothing kernel as music_video.py
    kern         = np.ones(40) / 40
    onset_smooth = np.convolve(onset_env / (onset_env.max() + 1e-10),
                               kern, mode="same")

    return duration, beat_times, times, onset_smooth, tempo_val


# ---------------------------------------------------------------------------
# Transition calculators
# ---------------------------------------------------------------------------

def section_transitions(duration: float, num_sections: int, fps: int) -> list[dict]:
    """Frame numbers where the Julia preset changes (section boundaries)."""
    section_dur = duration / num_sections
    events = []
    for n in range(1, num_sections):
        t     = n * section_dur
        frame = int(t * fps)
        # Which preset indices are involved (mirrors linspace logic)
        indices    = np.linspace(0, NUM_JULIA_PRESETS - 1, num_sections, dtype=int)
        from_preset = int(indices[n - 1])
        to_preset   = int(indices[n])
        events.append({
            "frame": frame,
            "time":  t,
            "type":  "julia_section",
            "note":  f"preset {from_preset} → {to_preset}",
        })
    return events


def palette_transitions(duration: float, num_sections: int, fps: int) -> list[dict]:
    """Frame numbers where the palette index increments."""
    section_dur  = duration / num_sections
    palette_dur  = section_dur * 2.5          # mirrors music_video.py
    total_frames = int(duration * fps)
    events       = []
    prev_idx     = 0

    # Step through time at frame resolution to catch every index change
    for fi in range(total_frames):
        t        = fi / fps
        pal_f    = t / palette_dur
        pal_idx  = int(pal_f) % NUM_PALETTES
        if pal_idx != prev_idx:
            events.append({
                "frame": fi,
                "time":  t,
                "type":  "palette",
                "note":  f"palette {prev_idx} → {pal_idx}",
            })
            prev_idx = pal_idx

    return events


def beat_events(beat_times: np.ndarray, fps: int) -> list[dict]:
    """One event per detected beat."""
    events = []
    for t in beat_times:
        events.append({
            "frame": int(t * fps),
            "time":  float(t),
            "type":  "beat",
            "note":  "",
        })
    return events


def onset_events(times: np.ndarray, onset_smooth: np.ndarray,
                 fps: int, threshold: float) -> list[dict]:
    """
    Peaks in the smoothed onset envelope above *threshold*.
    Uses a simple local-maximum detector with a 0.1 s minimum separation.
    """
    min_gap_frames = max(1, int(0.10 * fps))   # 100 ms minimum between peaks
    events = []
    n = len(times)

    for i in range(1, n - 1):
        if onset_smooth[i] >= threshold:
            # Local max within ±min_gap window
            lo = max(0, i - min_gap_frames)
            hi = min(n, i + min_gap_frames + 1)
            if onset_smooth[i] == onset_smooth[lo:hi].max():
                t = float(times[i])
                events.append({
                    "frame":    int(t * fps),
                    "time":     t,
                    "type":     "onset",
                    "note":     f"strength={onset_smooth[i]:.3f}",
                })

    return events


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

COLUMN_WIDTHS = {
    "frame": 8,
    "time":  10,
    "type":  15,
    "note":  30,
}

def print_table(events: list[dict], fps: int, duration: float, tempo: float,
                num_sections: int):
    total_frames = int(duration * fps)
    print()
    print("=" * 65)
    print(f"  Transition Report")
    print(f"  Duration : {duration:.2f} s  ({total_frames} frames @ {fps} fps)")
    print(f"  Tempo    : {tempo:.1f} BPM")
    print(f"  Sections : {num_sections}  (Julia presets)")
    print(f"  Events   : {len(events)}")
    print("=" * 65)

    header = (f"{'FRAME':>8}  {'TIME (s)':>9}  {'TYPE':<15}  NOTE")
    print(header)
    print("-" * 65)

    for e in events:
        note = e["note"] or ""
        print(f"{e['frame']:>8}  {e['time']:>9.3f}  {e['type']:<15}  {note}")

    print("-" * 65)
    print()


def write_csv(events: list[dict], path: str, fps: int, duration: float,
              tempo: float, num_sections: int):
    import csv
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["# duration_s", "fps", "tempo_bpm", "sections"])
        writer.writerow([f"{duration:.4f}", fps, f"{tempo:.2f}", num_sections])
        writer.writerow([])
        writer.writerow(["frame", "time_s", "type", "note"])
        for e in events:
            writer.writerow([e["frame"], f"{e['time']:.4f}", e["type"], e["note"]])
    print(f"CSV saved to: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Print fractal music video transition points in frames."
    )
    parser.add_argument("--audio",   "-a", required=True,
                        help="Path to the audio file.")
    parser.add_argument("--fps",           type=int,   default=30,
                        help="Frame rate used for frame-number calculations "
                             "(default: 30).")
    parser.add_argument("--sections", "-s", type=int,  default=0,
                        help="Number of Julia sections (0 = auto, same rule "
                             "as music_video.py).")
    parser.add_argument("--onset-threshold", type=float, default=0.4,
                        help="Smoothed onset strength threshold for onset "
                             "events, 0–1 (default: 0.4).")
    parser.add_argument("--no-beats",   action="store_true",
                        help="Omit beat events from output.")
    parser.add_argument("--no-onsets",  action="store_true",
                        help="Omit onset events from output.")
    parser.add_argument("--csv",        default="",
                        help="Optional path to save a CSV of the results.")

    args = parser.parse_args()

    if not os.path.isfile(args.audio):
        sys.exit(f"Error: audio file not found: {args.audio}")

    duration, beat_times, times, onset_smooth, tempo = analyze_audio(args.audio)

    # Auto section count mirrors music_video.py
    num_sections = args.sections
    if num_sections <= 0:
        num_sections = min(NUM_JULIA_PRESETS, max(3, int(duration / 15)))

    fps = args.fps

    events = []
    events += section_transitions(duration, num_sections, fps)
    events += palette_transitions(duration, num_sections, fps)
    if not args.no_beats:
        events += beat_events(beat_times, fps)
    if not args.no_onsets:
        events += onset_events(times, onset_smooth, fps, args.onset_threshold)

    # Sort by frame, then type for stable ordering of same-frame events
    type_order = {"julia_section": 0, "palette": 1, "beat": 2, "onset": 3}
    events.sort(key=lambda e: (e["frame"], type_order.get(e["type"], 9)))

    print_table(events, fps, duration, tempo, num_sections)

    if args.csv:
        write_csv(events, args.csv, fps, duration, tempo, num_sections)


if __name__ == "__main__":
    main()
