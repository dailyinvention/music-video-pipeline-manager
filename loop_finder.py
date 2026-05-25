#!/usr/bin/env python3
"""
Loop Finder – Find seamless loop points in a video or audio file.

Analyzes the audio track using chroma self-similarity to find pairs of
timestamps where the audio matches closely enough for a seamless loop.
Optionally exports the looped clip with ffmpeg.

Usage
─────
  python loop_finder.py -i video.mp4
  python loop_finder.py -i video.mp4 --candidates 10
  python loop_finder.py -i video.mp4 --min-duration 20 --max-duration 120
  python loop_finder.py -i video.mp4 --export loop.mp4
  python loop_finder.py -i video.mp4 --export loop.mp4 --pick 2
  python loop_finder.py -i audio.mp3
"""

import argparse
import os
import subprocess
import sys
import tempfile

import numpy as np
import librosa


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

def extract_audio(input_path: str) -> tuple[np.ndarray, int]:
    """
    Load audio from a video or audio file.
    For video files, extracts the audio track via ffmpeg first.
    Returns (y, sr).
    """
    ext = os.path.splitext(input_path)[1].lower()
    audio_exts = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}

    if ext in audio_exts:
        print(f"Loading audio: {input_path}")
        y, sr = librosa.load(input_path, sr=None, mono=True)
        return y, sr

    # Extract audio from video via ffmpeg
    print(f"Extracting audio from video: {input_path}")
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vn", "-ac", "1", "-ar", "44100",
        "-f", "wav", tmp.name,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        os.unlink(tmp.name)
        sys.exit(f"ffmpeg audio extraction failed:\n{result.stderr}")

    y, sr = librosa.load(tmp.name, sr=None, mono=True)
    os.unlink(tmp.name)
    return y, sr


# ---------------------------------------------------------------------------
# Loop point analysis
# ---------------------------------------------------------------------------

def find_loop_points(
    y: np.ndarray,
    sr: int,
    min_duration: float = 10.0,
    max_duration: float | None = None,
    n_candidates: int = 5,
    fps: float = 30.0,
) -> list[dict]:
    """
    Find candidate loop points using chroma + MFCC self-similarity.

    Strategy:
      1. Compute beat-synchronised chroma and MFCC features.
      2. Build a cosine self-similarity matrix.
      3. Scan for (start, end) pairs where:
           - audio at `end` sounds similar to audio at `start` (good splice)
           - loop duration is within [min_duration, max_duration]
      4. Rank by similarity score and return top candidates.
    """
    duration = librosa.get_duration(y=y, sr=sr)
    if max_duration is None:
        max_duration = duration

    print(f"  Duration : {duration:.2f}s")

    # Beat tracking for beat-aligned candidates
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    tempo_val = float(np.atleast_1d(tempo)[0])
    print(f"  Tempo    : {tempo_val:.1f} BPM  |  Beats: {len(beat_times)}")

    # Chroma features (harmonic content — key, chord)
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    # MFCC (timbral texture)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
    mfcc = librosa.util.normalize(mfcc, axis=1)

    # Combine into a single feature matrix (chroma + MFCC)
    features = np.vstack([chroma, mfcc])  # shape: (25, T)

    # Frame times for the feature matrix
    frame_times = librosa.frames_to_time(
        np.arange(features.shape[1]), sr=sr, hop_length=hop
    )

    # Normalise columns for cosine similarity
    norms = np.linalg.norm(features, axis=0, keepdims=True) + 1e-10
    features_norm = features / norms

    print("  Computing self-similarity matrix...")

    # We don't need the full matrix — just score each beat pair
    candidates = []

    for end_time in beat_times:
        for start_time in beat_times:
            loop_dur = end_time - start_time
            if loop_dur < min_duration or loop_dur > max_duration:
                continue

            # Find nearest feature frames
            start_idx = np.argmin(np.abs(frame_times - start_time))
            end_idx   = np.argmin(np.abs(frame_times - end_time))

            # Similarity: how well does `end` sound like `start`
            # (the splice point — end of loop back to beginning)
            splice_sim = float(
                features_norm[:, end_idx] @ features_norm[:, start_idx]
            )

            # Also score how consistent the audio is just around the splice
            # (a few frames before end vs a few frames after start)
            window = max(1, int(0.5 * sr / hop))  # ~0.5s window
            pre_end   = features_norm[:, max(0, end_idx - window):end_idx]
            post_start = features_norm[:, start_idx:start_idx + window]
            if pre_end.shape[1] > 0 and post_start.shape[1] > 0:
                context_sim = float(
                    np.mean(pre_end.T @ post_start / pre_end.shape[1])
                )
            else:
                context_sim = splice_sim

            score = 0.6 * splice_sim + 0.4 * context_sim

            candidates.append({
                "start_time":  start_time,
                "end_time":    end_time,
                "duration":    loop_dur,
                "score":       score,
                "start_frame": int(start_time * fps),
                "end_frame":   int(end_time * fps),
            })

    if not candidates:
        sys.exit(
            "No loop candidates found. Try reducing --min-duration "
            "or widening --max-duration."
        )

    # Sort by score descending, deduplicate by proximity (>2s apart)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    filtered = []
    for c in candidates:
        too_close = any(
            abs(c["start_time"] - f["start_time"]) < 2.0 and
            abs(c["end_time"]   - f["end_time"])   < 2.0
            for f in filtered
        )
        if not too_close:
            filtered.append(c)
        if len(filtered) >= n_candidates:
            break

    return filtered


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_candidates(candidates: list[dict], fps: float):
    print()
    print("=" * 65)
    print("  Loop Candidates  (ranked by similarity score)")
    print("=" * 65)
    print(f"  {'#':<4} {'START':>8} {'END':>8} {'DURATION':>10} "
          f"{'SCORE':>7}  FRAMES")
    print("-" * 65)
    for i, c in enumerate(candidates, 1):
        start_ts = _fmt_time(c["start_time"])
        end_ts   = _fmt_time(c["end_time"])
        dur_ts   = _fmt_time(c["duration"])
        print(f"  {i:<4} {start_ts:>8} {end_ts:>8} {dur_ts:>10} "
              f"  {c['score']:.3f}  "
              f"frames {c['start_frame']}–{c['end_frame']}")
    print("-" * 65)
    print()


def _fmt_time(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_loop(
    input_path: str,
    output_path: str,
    candidate: dict,
    codec: str = "h264",
    crf: int = 18,
    crossfade: float = 0.0,
    fade: float = 0.0,
):
    start  = candidate["start_time"]
    end    = candidate["end_time"]
    dur    = candidate["duration"]
    vcodec = "libx265" if codec.lower() in ("h265", "hevc") else "libx264"

    print(f"Exporting loop: {_fmt_time(start)} → {_fmt_time(end)} "
          f"({_fmt_time(dur)}) to {output_path}")

    if fade > 0 and fade * 2 >= dur:
        sys.exit(
            f"Error: fade ({fade}s) is too long for loop "
            f"duration ({dur:.2f}s). Use a shorter --fade value."
        )

    if crossfade > 0:
        if crossfade * 2 >= dur:
            sys.exit(
                f"Error: crossfade ({crossfade}s) is too long for loop "
                f"duration ({dur:.2f}s). Use a shorter --crossfade value."
            )
        _export_with_crossfade(
            input_path, output_path,
            start, end, dur, crossfade, vcodec, crf, fade,
        )
    else:
        _export_plain(
            input_path, output_path,
            start, end, dur, vcodec, crf, fade,
        )

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Done! Saved to: {output_path} ({size_mb:.1f} MB)")
    print(f"  Loop : {_fmt_time(start)} → {_fmt_time(end)}  "
          f"({_fmt_time(dur)})  score={candidate['score']:.3f}")
    if crossfade > 0:
        print(f"  Crossfade: {crossfade}s baked into loop seam")
    if fade > 0:
        print(f"  Fade in/out: {fade}s (video + audio)")


def _export_plain(
    input_path: str,
    output_path: str,
    start: float,
    end: float,
    dur: float,
    vcodec: str,
    crf: int,
    fade: float = 0.0,
):
    if fade > 0:
        vf = (
            f"fade=t=in:st=0:d={fade},"
            f"fade=t=out:st={dur - fade}:d={fade}"
        )
        af = (
            f"afade=t=in:st=0:d={fade},"
            f"afade=t=out:st={dur - fade}:d={fade}"
        )
        print(f"  Applying {fade}s fade in + fade out (video + audio)...")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-to", str(end),
            "-i", input_path,
            "-vf", vf, "-af", af,
            "-c:v", vcodec, "-crf", str(crf),
            "-preset", "slow", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-to", str(end),
            "-i", input_path,
            "-c:v", vcodec, "-crf", str(crf),
            "-preset", "slow", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart",
            output_path,
        ]
    _run(cmd)


def _export_with_crossfade(
    input_path: str,
    output_path: str,
    start: float,
    end: float,
    dur: float,
    crossfade: float,
    vcodec: str,
    crf: int,
    fade: float = 0.0,
):
    """
    Bake a crossfade at the loop seam so playback loops smoothly.

    Output structure (duration = dur seconds, same as plain export):
      [0 … dur-crossfade]  main body — unchanged
      [dur-crossfade … dur]  crossfade: tail fades OUT while head fades IN

    When the player loops back to t=0, the head content that was fading
    in at the end matches the opening frames exactly → seamless loop.

    If fade > 0, also applies a fade-in from black at the start and
    fade-out to black at the end (both video and audio).
    """
    print(f"  Applying {crossfade}s crossfade at loop seam...")
    if fade > 0:
        print(f"  Applying {fade}s fade in + fade out (video + audio)...")

    C  = crossfade
    D  = dur
    # The final duration after crossfade is D - C
    final_dur = D - C

    # Video fade-in/out applied to the final [vout] stream
    vfade = ""
    afade = ""
    if fade > 0:
        vfade = (
            f"[vout]fade=t=in:st=0:d={fade},"
            f"fade=t=out:st={final_dur - fade}:d={fade}[vfinal];"
        )
        afade = (
            f"[aout]afade=t=in:st=0:d={fade},"
            f"afade=t=out:st={final_dur - fade}:d={fade}[afinal]"
        )

    fc = (
        # ── VIDEO ──────────────────────────────────────────────────────
        f"[0:v]split=3[v1][v2][v3];"
        # main body: from 0 to D-C
        f"[v1]trim=0:{D - C},setpts=PTS-STARTPTS[vmain];"
        # tail: last C seconds (fades out)
        f"[v2]trim={D - C}:{D},setpts=PTS-STARTPTS[vtail];"
        # head: first C seconds (fades in)
        f"[v3]trim=0:{C},setpts=PTS-STARTPTS[vhead];"
        # crossfade tail → head
        f"[vtail][vhead]xfade=transition=fade:duration={C}:offset=0[vcross];"
        # join main body + crossfade section
        f"[vmain][vcross]concat=n=2:v=1:a=0[vout];"
        # ── AUDIO ──────────────────────────────────────────────────────
        f"[0:a]asplit=3[a1][a2][a3];"
        f"[a1]atrim=0:{D - C},asetpts=PTS-STARTPTS[amain];"
        f"[a2]atrim={D - C}:{D},asetpts=PTS-STARTPTS[atail];"
        f"[a3]atrim=0:{C},asetpts=PTS-STARTPTS[ahead];"
        f"[atail][ahead]acrossfade=d={C}[across];"
        f"[amain][across]concat=n=2:v=0:a=1[aout]"
    )

    if fade > 0:
        # Replace trailing [aout] with semicolon, then append fade filters
        fc = fc + ";" + vfade + afade
        vmap = "[vfinal]"
        amap = "[afinal]"
    else:
        vmap = "[vout]"
        amap = "[aout]"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end),
        "-i", input_path,
        "-filter_complex", fc,
        "-map", vmap, "-map", amap,
        "-c:v", vcodec, "-crf", str(crf),
        "-preset", "slow", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "320k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run(cmd)


def _run(cmd: list[str]):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg stderr:\n{result.stderr}")
        sys.exit(f"Error: ffmpeg failed (exit code {result.returncode})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find seamless loop points in a video or audio file."
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Input video or audio file.",
    )
    parser.add_argument(
        "--candidates", "-n", type=int, default=5,
        help="Number of loop candidates to show (default: 5).",
    )
    parser.add_argument(
        "--min-duration", type=float, default=10.0,
        help="Minimum loop duration in seconds (default: 10).",
    )
    parser.add_argument(
        "--max-duration", type=float, default=None,
        help="Maximum loop duration in seconds (default: full length).",
    )
    parser.add_argument(
        "--fps", type=float, default=30.0,
        help="Frame rate for frame number output (default: 30).",
    )
    parser.add_argument(
        "--export", "-o", default="",
        help="Export the chosen loop as a video file.",
    )
    parser.add_argument(
        "--pick", type=int, default=1,
        help="Which candidate to export (1 = best, default: 1).",
    )
    parser.add_argument(
        "--codec", choices=["h264", "h265"], default="h264",
        help="Output codec for export (default: h264).",
    )
    parser.add_argument(
        "--crf", type=int, default=18,
        help="Output quality for export (default: 18).",
    )
    parser.add_argument(
        "--crossfade", type=float, default=0.0,
        help="Crossfade duration in seconds baked into the loop seam. "
             "The tail fades out while the head fades in so the loop "
             "plays back seamlessly (default: 0 = no crossfade).",
    )
    parser.add_argument(
        "--fade", type=float, default=0.0,
        help="Fade-in and fade-out duration in seconds (equal). "
             "Fades video to/from black and audio to/from silence "
             "(default: 0 = no fade).",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    # Auto-detect fps from video
    fps = args.fps
    ext = os.path.splitext(args.input)[1].lower()
    if ext not in {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}:
        import cv2
        cap = cv2.VideoCapture(args.input)
        detected = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if detected > 0:
            fps = detected

    y, sr = extract_audio(args.input)

    print("\nAnalyzing loop points...")
    candidates = find_loop_points(
        y, sr,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        n_candidates=args.candidates,
        fps=fps,
    )

    print_candidates(candidates, fps)

    if args.export:
        pick = max(1, min(args.pick, len(candidates)))
        export_loop(
            args.input,
            args.export,
            candidates[pick - 1],
            codec=args.codec,
            crf=args.crf,
            crossfade=args.crossfade,
            fade=args.fade,
        )


if __name__ == "__main__":
    main()
