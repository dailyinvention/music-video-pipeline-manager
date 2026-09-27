#!/usr/bin/env python3
"""
Audio Bass, Distortion & Digital Blip Fixer for Music Tracks

Applies calibrated low-end filtering, de-clicking, smooth intro/outro fades, and two-pass EBU
R128 loudness normalization to eliminate digital blips/pops (common in AI/Suno exports) and
prevent bass/bass-kick distortion and clipping across music tracks.

Processing steps applied:
  1. Highpass filter @ 28 Hz (poles=2): Removes sub-rumble, DC offset, and subsonic thumps.
  2. De-click filter (adeclick): Eliminates impulsive digital spikes and buffer glitch clicks.
  3. Intro glitch detection: Mutes an isolated pre-onset blip (a spike followed by a stretch
     of near-total silence before the track's real onset -- common in AI/Suno exports) outright,
     since a 50ms fade can't mask something sitting in near-silence ahead of the real start.
  4. Fade-In (default 0.4s): Removes start-of-file buffer pops and non-zero crossing clicks, and
     softens the entry into the track's real musical onset so it doesn't hard-cut in at full volume.
  5. Smooth Fade-Out (default 2.5s): Prevents abrupt end truncation clicks while keeping smooth musical decay.
  6. Equalizer @ 55 Hz (Q=1.2, -4.5 dB): Controls deep sub-kick resonances.
  7. Equalizer @ 80 Hz (Q=1.4, -3.0 dB): Tames upper bass kick thump.
  8. Two-pass loudnorm (EBU R128): normalizes to -14 LUFS integrated loudness with a -1.5 dBTP
     true-peak ceiling -- unlike simple sample-peak limiting or MP3Gain (which has no concept of
     true peak and can ride loudness right up to 0 dBFS), this explicitly caps the inter-sample
     peaks that cause audible distortion on decoders like VLC even when other players mask it.
  9. Final safety limiter (asoftclip): Guarantees the true-peak ceiling is actually enforced --
     loudnorm's linear-mode gain has no per-sample ceiling, so a hard transient can still get
     pushed past 0 dBFS after that gain is applied.

Usage:
  # Process a single file (creates a .original backup by default)
  python tools/fix_audio_bass.py "/path/to/track.mp3"

  # Process all audio files in a directory recursively:
  python tools/fix_audio_bass.py "/Volumes/Music To Sleep To" --recursive

  # Customize fade in / fade out durations (e.g., 0.1s intro fade, 3.5s outro fade):
  python tools/fix_audio_bass.py "/path/to/track.mp3" --fade-in 0.1 --fade-out 3.5

  # Disable fades if not needed:
  python tools/fix_audio_bass.py "/path/to/track.mp3" --no-fades

  # Dry run (see what files would be processed without modifying):
  python tools/fix_audio_bass.py "/Volumes/Music To Sleep To" --recursive --dry-run
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Supported audio extensions
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aiff"}


def is_ffmpeg_available() -> bool:
    """Check if ffmpeg is accessible on PATH."""
    return shutil.which("ffmpeg") is not None


def is_ffprobe_available() -> bool:
    """Check if ffprobe is accessible on PATH."""
    return shutil.which("ffprobe") is not None


def measure_loudness(
    file_path: Path,
    filter_chain: str,
    integrated_target: float,
    true_peak_target: float,
    lra_target: float,
) -> dict | None:
    """
    First pass of two-pass EBU R128 loudness normalization: run the file
    through the same (non-loudnorm) filter chain that will be used for the
    real encode, then measure its loudness/true-peak/LRA via ffmpeg's
    loudnorm filter in analysis mode. Returns the parsed measurement dict,
    or None if measurement failed.
    """
    if not is_ffmpeg_available():
        return None

    measure_filter = (
        f"{filter_chain},loudnorm=I={integrated_target}:TP={true_peak_target}:"
        f"LRA={lra_target}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-v", "info",
        "-i", str(file_path),
        "-af", measure_filter,
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        output = res.stderr
        start = output.rfind("{")
        end = output.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        import json
        return json.loads(output[start:end + 1])
    except Exception:
        return None


def get_audio_duration(file_path: Path) -> float | None:
    """Get the duration of an audio file in seconds using ffprobe."""
    if not is_ffprobe_available():
        return None
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    return None


def detect_tail_cutoff_glitch(file_path: Path, duration: float | None = None) -> float | None:
    """
    Detect abrupt cutoff blips/pops that frequently occur near the end of AI-generated tracks (e.g. Suno).
    Returns the timestamp in seconds where the cutoff spike begins, or None if no glitch is detected.
    """
    if not duration or duration < 5.0 or not is_ffmpeg_available():
        return None
    scan_sec = min(duration, 15.0)
    cmd = [
        "ffmpeg", "-v", "error",
        "-sseof", f"-{scan_sec}",
        "-i", str(file_path),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ar", "44100", "-ac", "1",
        "-"
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw, _ = p.communicate()
        if not raw:
            return None
        import array
        samples = array.array("f")
        samples.frombytes(raw)

        sr = 44100
        win_size = int(0.01 * sr)  # 10ms window
        n_windows = len(samples) // win_size
        if n_windows < 20:
            return None

        start_time = duration - (len(samples) / sr)
        peaks = []
        times = []
        for i in range(n_windows):
            chunk = samples[i * win_size : (i + 1) * win_size]
            pk = max(abs(x) for x in chunk)
            peaks.append(pk)
            times.append(start_time + (i * win_size) / sr)

        # Search backwards from the end within the last 8 seconds
        for i in range(len(peaks) - 1, -1, -1):
            t_from_end = duration - times[i]
            if t_from_end > 8.0:
                break
            if peaks[i] > 0.035:
                trailing = peaks[min(len(peaks), i + 30):]
                if len(trailing) > 0 and (sum(trailing) / len(trailing)) < 0.008 and max(trailing) < 0.02:
                    onset_idx = i
                    for k in range(i, max(0, i - 20), -1):
                        if peaks[k] < 0.01:
                            onset_idx = k
                            break
                        onset_idx = k
                    return times[onset_idx]
    except Exception:
        pass
    return None


def detect_intro_glitch(file_path: Path, duration: float | None = None) -> float | None:
    """
    Detect an isolated blip/pop near the very start of a track (common in AI/Suno
    exports): a brief low-level spike within the first ~200ms, followed by a stretch
    of near-total digital silence before the track's real musical onset begins. Unlike
    a deliberate slow fade-in (which ramps up steadily), this pattern is a spike
    immediately followed by silence, then a separate later onset -- the standard 50ms
    fade-in is too short/shallow to mask it. Returns the timestamp (seconds) through
    which audio should be muted to remove it, or None if no such glitch is detected.
    """
    if not duration or duration < 2.0 or not is_ffmpeg_available():
        return None
    scan_sec = min(duration, 3.0)
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", str(file_path),
        "-t", f"{scan_sec}",
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ar", "44100", "-ac", "1",
        "-"
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw, _ = p.communicate()
        if not raw:
            return None
        import array
        samples = array.array("f")
        samples.frombytes(raw)

        sr = 44100
        win_size = int(0.005 * sr)  # 5ms window
        n_windows = len(samples) // win_size
        if n_windows < 40:
            return None

        peaks = [max(abs(x) for x in samples[i * win_size:(i + 1) * win_size]) for i in range(n_windows)]
        times = [i * win_size / sr for i in range(n_windows)]

        glitch_windows = int(0.2 / 0.005)   # look for the spike within the first 200ms
        quiet_windows = int(0.2 / 0.005)    # require >=200ms of near-silence right after it

        spike_idx = None
        for i in range(min(glitch_windows, n_windows)):
            if peaks[i] > 0.015:
                spike_idx = i
                break
        if spike_idx is None:
            return None

        # The blip itself typically spans a few consecutive windows (its rise/decay),
        # not just one -- skip past the whole event before checking for quiet.
        spike_end_idx = spike_idx
        max_spike_span = int(0.1 / 0.005)  # cap the blip itself at 100ms
        while spike_end_idx < min(spike_idx + max_spike_span, n_windows) and peaks[spike_end_idx] >= 0.008:
            spike_end_idx += 1

        quiet_start = spike_end_idx
        quiet_end = min(n_windows, quiet_start + quiet_windows)
        if quiet_end - quiet_start < quiet_windows:
            return None
        quiet_slice = peaks[quiet_start:quiet_end]
        if max(quiet_slice) >= 0.008:
            return None

        # Find where real content resumes after the quiet gap.
        onset_idx = quiet_end
        for i in range(quiet_end, n_windows):
            if peaks[i] >= 0.008:
                onset_idx = i
                break
        else:
            onset_idx = n_windows - 1

        return times[onset_idx]
    except Exception:
        pass
    return None


def detect_audio_silence_end(
    file_path: Path,
    silence_threshold_db: float = -50.0,
    min_silence_duration: float = 1.0,
) -> float | None:
    """
    Detect the timestamp where continuous silence begins at the end of the audio file.
    Returns the cutoff timestamp in seconds (where audio ends), or None if full audio is active.
    """
    if not is_ffmpeg_available():
        return None

    cmd = [
        "ffmpeg", "-v", "info",
        "-i", str(file_path),
        "-af", f"silencedetect=noise={silence_threshold_db}dB:d={min_silence_duration}",
        "-f", "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        output = res.stderr

        # Look for the last silence_start event
        silence_starts = []
        for line in output.splitlines():
            if "silence_start:" in line:
                try:
                    parts = line.split("silence_start:")
                    val = parts[1].strip().split()[0]
                    silence_starts.append(float(val))
                except (IndexError, ValueError):
                    pass

        if silence_starts:
            # Check if there is a silence_end after the last silence_start
            last_start = silence_starts[-1]
            last_start_idx = output.rfind(f"silence_start: {last_start}")
            end_idx = output.find("silence_end:", last_start_idx)
            # If silence extends to end of file (no silence_end after last start)
            if end_idx == -1:
                return last_start
    except Exception:
        pass
    return None


def build_filter_chain(
    duration: float | None = None,
    declick: bool = True,
    fade_in_sec: float = 0.4,
    fade_out_sec: float = 3.5,
    cutoff_glitch_time: float | None = None,
    trim_end_time: float | None = None,
    intro_glitch_time: float | None = None,
) -> str:
    """
    Construct FFmpeg audio filter graph including declicking, silence trimming, fades, cutoff blip muting, and bass equalization.
    """
    filters = []

    # 1. Trim trailing dead air / silence if detected
    effective_duration = duration
    if trim_end_time is not None:
        filters.append(f"atrim=0:{trim_end_time:.3f}")
        effective_duration = trim_end_time

    # 2. Subsonic highpass filter removes DC offset and subsonic blip energy
    filters.append("highpass=f=28:poles=2")

    # 3. De-click filter for impulsive digital artifacts and clicks
    if declick:
        filters.append("adeclick")

    # 4. Intro glitch handling: mute an isolated pre-onset blip outright (a 50ms
    # fade can't mask it since it sits in near-silence before the real onset), then
    # fade in starting right at the real musical onset.
    fade_in_start = 0.0
    if intro_glitch_time:
        filters.append(f"volume=enable='lte(t,{intro_glitch_time:.3f})':volume=0")
        fade_in_start = intro_glitch_time

    # 5. Micro-fade in at start to eliminate header pops / non-zero crossings
    if fade_in_sec > 0:
        filters.append(f"afade=t=in:ss={fade_in_start:.3f}:d={fade_in_sec:.3f}:curve=qsin")

    # 6. Smooth fade out & tail cutoff handling
    if cutoff_glitch_time:
        fade_end = max(0.0, cutoff_glitch_time - 0.1)
        actual_fade_dur = min(fade_out_sec, fade_end) if fade_out_sec > 0 else 0.0
        if actual_fade_dur > 0:
            fade_start = max(0.0, fade_end - actual_fade_dur)
            filters.append(f"afade=t=out:st={fade_start:.3f}:d={actual_fade_dur:.3f}:curve=hsin")
        filters.append(f"volume=enable='gte(t,{fade_end:.3f})':volume=0")
    elif effective_duration and fade_out_sec > 0 and effective_duration > fade_out_sec:
        start_fade_out = max(0.0, effective_duration - fade_out_sec)
        filters.append(f"afade=t=out:st={start_fade_out:.3f}:d={fade_out_sec:.3f}:curve=hsin")

    # 7. Bass EQ (peak/loudness normalization is applied separately via loudnorm)
    filters.append("equalizer=f=55:t=q:w=1.2:g=-4.5")
    filters.append("equalizer=f=80:t=q:w=1.4:g=-3.0")

    return ",".join(filters)


def collect_audio_files(target_path: Path, recursive: bool = False) -> list[Path]:
    """Collect all target audio files while ignoring existing backup files."""
    files = []
    if target_path.is_file():
        if target_path.suffix.lower() in AUDIO_EXTS:
            files.append(target_path)
    elif target_path.is_dir():
        pattern = "**/*" if recursive else "*"
        for p in target_path.glob(pattern):
            if not p.is_file():
                continue
            if p.suffix.lower() in AUDIO_EXTS:
                # Ignore previous backups
                if ".original." in p.name or p.name.endswith(".original"):
                    continue
                # Ignore hidden files or temp files
                if p.name.startswith("."):
                    continue
                files.append(p)
    return sorted(files)


def build_loudnorm_filter(
    measurement: dict | None,
    integrated_target: float,
    true_peak_target: float,
    lra_target: float,
) -> str:
    """
    Build the second-pass loudnorm filter string. Feeds the first pass's
    measured stats back in (linear mode) for an accurate single correction;
    falls back to single-pass dynamic normalization if measurement failed.
    """
    base = f"loudnorm=I={integrated_target}:TP={true_peak_target}:LRA={lra_target}"
    if not measurement:
        return base
    try:
        return (
            f"{base}:"
            f"measured_I={measurement['input_i']}:"
            f"measured_TP={measurement['input_tp']}:"
            f"measured_LRA={measurement['input_lra']}:"
            f"measured_thresh={measurement['input_thresh']}:"
            f"offset={measurement['target_offset']}:"
            f"linear=true"
        )
    except KeyError:
        return base


def process_audio_file(
    file_path: Path,
    output_dir: Path | None = None,
    no_backup: bool = False,
    bitrate: str = "320k",
    declick: bool = True,
    fade_in_sec: float = 0.4,
    fade_out_sec: float = 3.5,
    trim_silence: bool = True,
    keep_end_silence: float = 2.0,
    silence_threshold_db: float = -50.0,
    run_loudnorm: bool = True,
    loudness_target: float = -14.0,
    true_peak_target: float = -1.5,
    lra_target: float = 11.0,
    dry_run: bool = False,
) -> bool:
    """Process an audio file through FFmpeg with silence trimming, blip removal, smooth fades, bass filtering, and two-pass EBU R128 loudness normalization."""
    print(f"\n[•] Target: {file_path.name}")
    print(f"    Path:   {file_path}")

    duration = get_audio_duration(file_path)
    trim_end_time = None

    if trim_silence and duration and duration > 5.0:
        silence_start = detect_audio_silence_end(
            file_path,
            silence_threshold_db=silence_threshold_db,
            min_silence_duration=1.0,
        )
        if silence_start is not None:
            trailing_silence_dur = duration - silence_start
            # Only trim if trailing silence is longer than what we want to keep + 1.0s buffer
            if trailing_silence_dur > (keep_end_silence + 1.0):
                trim_end_time = silence_start + keep_end_silence
                print(f"    [✂] Trimming trailing silence: {trailing_silence_dur:.1f}s found -> keeping {keep_end_silence:.1f}s (new length: {trim_end_time:.2f}s)")

    eff_dur = trim_end_time if trim_end_time is not None else duration
    cutoff_glitch = None
    if eff_dur and fade_out_sec > 0 and trim_end_time is None:
        cutoff_glitch = detect_tail_cutoff_glitch(file_path, duration)

    intro_glitch = detect_intro_glitch(file_path, duration) if fade_in_sec > 0 else None

    if duration:
        glitch_msg = f" | Tail Glitch Muted: @ {cutoff_glitch:.2f}s" if cutoff_glitch else ""
        intro_msg = f" | Intro Glitch Muted: 0-{intro_glitch:.2f}s" if intro_glitch else ""
        print(f"    Duration: {duration:.2f}s | Fade In: {fade_in_sec}s | Fade Out: {fade_out_sec}s | De-click: {'On' if declick else 'Off'}{glitch_msg}{intro_msg}")

    filter_chain = build_filter_chain(
        duration=duration,
        declick=declick,
        fade_in_sec=fade_in_sec,
        fade_out_sec=fade_out_sec,
        cutoff_glitch_time=cutoff_glitch,
        trim_end_time=trim_end_time,
        intro_glitch_time=intro_glitch,
    )

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        dest_file = output_dir / file_path.name
    else:
        dest_file = file_path

    final_filter = filter_chain
    if run_loudnorm:
        if dry_run:
            print(f"    [DRY RUN] Would measure & apply loudnorm: I={loudness_target} TP={true_peak_target} LRA={lra_target}")
        else:
            print(f"    [•] Measuring loudness (pass 1/2)...")
            measurement = measure_loudness(file_path, filter_chain, loudness_target, true_peak_target, lra_target)
            if measurement:
                print(f"    Measured: I={measurement.get('input_i')} LUFS, TP={measurement.get('input_tp')} dBTP, LRA={measurement.get('input_lra')}")
            else:
                print(f"    [!] Loudness measurement failed; falling back to single-pass dynamic normalization.")
            final_filter = f"{filter_chain},{build_loudnorm_filter(measurement, loudness_target, true_peak_target, lra_target)}"

    # Final safety limiter: loudnorm's linear-mode gain is a fixed multiplier derived
    # from a separate analysis pass, so it has no per-sample ceiling -- a hard transient
    # (e.g. a kick-drum onset) can still get pushed past 0 dBFS after that gain is
    # applied, causing an audible click/blip. ffmpeg's `alimiter` does not actually
    # engage on fast transients like this on all builds, so use `asoftclip` instead,
    # which reliably enforces the ceiling (verified via decode round-trip).
    true_peak_linear = 10 ** (true_peak_target / 20)
    final_filter = f"{final_filter},asoftclip=type=tanh:threshold={true_peak_linear:.6f}:oversample=8"

    if dry_run:
        print(f"    [DRY RUN] Would process with filter: {final_filter}")
        print(f"    [DRY RUN] Would output to: {dest_file}")
        return True

    # Temporary output file in the same directory to allow atomic replace
    temp_output = dest_file.parent / f".tmp_{file_path.stem}{file_path.suffix}"

    # Determine audio codec & options based on extension
    ext = file_path.suffix.lower()
    codec_args = []
    if ext == ".mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif ext == ".wav":
        codec_args = ["-c:a", "pcm_s16le"]
    elif ext == ".flac":
        codec_args = ["-c:a", "flac"]
    elif ext == ".m4a":
        codec_args = ["-c:a", "aac", "-b:a", "256k"]
    else:
        codec_args = ["-c:a", "libmp3lame", "-b:a", bitrate]

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(file_path),
        "-af", final_filter,
        *codec_args,
        str(temp_output),
    ]

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if res.returncode != 0:
            print(f"    [!] FFmpeg error on {file_path.name}:\n{res.stderr[-400:]}")
            if temp_output.exists():
                temp_output.unlink(missing_ok=True)
            return False

        # If in-place replacement, handle backup
        if output_dir is None:
            backup_path = file_path.parent / f"{file_path.stem}.original{file_path.suffix}"
            if not no_backup and not backup_path.exists():
                shutil.copy2(file_path, backup_path)
                print(f"    Backup created: {backup_path.name}")

        # Atomically replace/move file to destination
        temp_output.replace(dest_file)
        print(f"    [✓] Audio processing complete: {dest_file.name}")
        return True

    except Exception as e:
        print(f"    [!] Failed to process {file_path.name}: {e}")
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Fix bass/kick distortion, trim dead silence, remove digital blips/clicks, apply intro/outro fades, and apply two-pass EBU R128 loudness normalization across audio tracks."
    )
    parser.add_argument(
        "target",
        type=str,
        help="Path to an audio file or a folder containing audio files",
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Recursively scan subdirectories for audio files",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default=None,
        help="Optional output directory. If omitted, modifies files in-place with backup",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create .original backup files when modifying in-place",
    )
    parser.add_argument(
        "-b", "--bitrate",
        type=str,
        default="320k",
        help="Bitrate for MP3 encoding (default: 320k)",
    )
    parser.add_argument(
        "--fade-in",
        type=float,
        default=0.4,
        help="Fade-in duration in seconds for smooth intro / click prevention and to avoid a hard musical entry (default: 0.4s)",
    )
    parser.add_argument(
        "--fade-out",
        type=float,
        default=3.5,
        help="Fade-out duration in seconds for smooth outro / end-click prevention (default: 3.5s)",
    )
    parser.add_argument(
        "--no-fades",
        action="store_true",
        help="Disable start and end fades",
    )
    parser.add_argument(
        "--no-trim-silence",
        action="store_true",
        help="Disable automatic trailing silence removal",
    )
    parser.add_argument(
        "--keep-end-silence",
        type=float,
        default=2.0,
        help="Target seconds of natural silence/decay padding to keep at track end (default: 2.0s)",
    )
    parser.add_argument(
        "--silence-threshold",
        type=float,
        default=-50.0,
        help="dB threshold below which audio is considered silence (default: -50.0 dB)",
    )
    parser.add_argument(
        "--no-declick",
        action="store_true",
        help="Disable the FFmpeg de-click (adeclick) filter",
    )
    parser.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip loudness normalization",
    )
    parser.add_argument(
        "--loudness-target",
        type=float,
        default=-14.0,
        help="Integrated loudness target in LUFS (default: -14.0, the common streaming-platform target)",
    )
    parser.add_argument(
        "--true-peak",
        type=float,
        default=-1.5,
        help="True-peak ceiling in dBTP -- keeps loudness normalization from producing inter-sample peaks that clip on some decoders (default: -1.5 dBTP)",
    )
    parser.add_argument(
        "--loudness-range",
        type=float,
        default=11.0,
        help="Target loudness range (LRA) for the loudnorm filter (default: 11.0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matched files without making any changes",
    )

    args = parser.parse_args()

    if not is_ffmpeg_available():
        print("Error: 'ffmpeg' command not found. Please make sure FFmpeg is installed.")
        sys.exit(1)

    target_path = Path(args.target).resolve()
    if not target_path.exists():
        print(f"Error: Target path does not exist: {target_path}")
        sys.exit(1)

    files = collect_audio_files(target_path, recursive=args.recursive)
    if not files:
        print(f"No matching audio files found at {target_path}")
        sys.exit(0)

    print(f"Found {len(files)} audio file(s) to process.")
    out_dir = Path(args.output_dir).resolve() if args.output_dir else None

    fade_in = 0.0 if args.no_fades else args.fade_in
    fade_out = 0.0 if args.no_fades else args.fade_out
    declick = not args.no_declick

    success_count = 0
    for f in files:
        ok = process_audio_file(
            file_path=f,
            output_dir=out_dir,
            no_backup=args.no_backup,
            bitrate=args.bitrate,
            declick=declick,
            fade_in_sec=fade_in,
            fade_out_sec=fade_out,
            run_loudnorm=not args.no_loudnorm,
            loudness_target=args.loudness_target,
            true_peak_target=args.true_peak,
            lra_target=args.loudness_range,
            dry_run=args.dry_run,
        )
        if ok:
            success_count += 1

    print(f"\nDone! Processed {success_count}/{len(files)} file(s) successfully.")


if __name__ == "__main__":
    main()
