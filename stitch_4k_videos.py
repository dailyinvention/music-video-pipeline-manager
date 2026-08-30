#!/usr/bin/env python3
"""
4K Video Stitcher & Transition Pipeline with Persistent Logo, Presentation Typography & YouTube Chapter Export

Features:
- Scans `/Volumes/Music To Sleep To/` for all `* 4k (Video).mp4` files.
- Applies 5-second fade-in, 5-second fade-out, and 5-second black gap transitions.
- Keeps the official "Music To Sleep To" logo steadily visible in the bottom-right corner across all transitions.
- Matches each folder/video 100% against `pipeline_data/pipeline.db` by name, folder, and filename.
- Displays right-aligned presentation typography in the top-right corner using the album serif font (Baskerville):
    Line 1: [Track Title] (e.g. "A Forgotten City At Rest")
    Line 2: Hypnosonica Presents: Music To Sleep To
    Line 3: "[Bedtime Sleep Quote from Database]"
    Line 4: 432Hz Deep Sleep & Relaxation (Audio / Frequency Badge)
- Automatically captures the exact start time of each track and exports YouTube chapter bookmarks
  to `[output]_chapters.txt` (ready to copy/paste directly into your YouTube description).
- Encodes with Apple Silicon hardware acceleration (`hevc_videotoolbox`) in H.265 (HEVC).
- Resumable checkpointing (.stitch_cache) and fast lossless final assembly.

Usage:
    python stitch_4k_videos.py                                   # Run full compilation + export chapters
    python stitch_4k_videos.py --chapters-only                   # Export YouTube chapters text file immediately
    python stitch_4k_videos.py --dry-run                         # Preview track titles, quotes, timestamps, and durations
    python stitch_4k_videos.py --limit 3                         # Test on first 3 videos
    python stitch_4k_videos.py --badge-text "Theta Wave Meditation • 4K UHD" # Custom badge text
    python stitch_4k_videos.py --text-display full               # Keep text on screen for entire track
"""

import sys
import os
import re
import json
import time
import shutil
import sqlite3
import random
import argparse
import subprocess
import signal
from PIL import Image, ImageDraw, ImageFont

# Ensure immediate unbuffered output for terminals and background logs
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# Default Paths
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_DIR = "/Volumes/Music To Sleep To"
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/Documents")
DEFAULT_OUTPUT_FILENAME = "Music_To_Sleep_To_4K_Compilation.mp4"
DEFAULT_CACHE_DIR_EXTERNAL = os.path.join(DEFAULT_INPUT_DIR, ".stitch_cache")
DEFAULT_CACHE_DIR_LOCAL = os.path.expanduser("~/Documents/.stitch_cache")
DEFAULT_WATERMARK = os.path.join(ROOT, "music_to_sleep_to_profile.png")
DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")
DEFAULT_FONT = "Baskerville"
DEFAULT_PRESENTATION_SERIES = "Hypnosonica Presents: Music To Sleep To"
DEFAULT_BADGE_TEXT = "432Hz Deep Sleep & Relaxation"

# Global flag for graceful interrupt handling
INTERRUPTED = False

def handle_sigint(signum, frame):
    global INTERRUPTED
    print("\n\n⚠️ Process interrupted by user (SIGINT/SIGTERM). Saving state and exiting gracefully...")
    INTERRUPTED = True

signal.signal(signal.SIGINT, handle_sigint)
signal.signal(signal.SIGTERM, handle_sigint)


def format_timestamp(seconds: float) -> str:
    """Format seconds into standard YouTube timestamp format (HH:MM:SS or MM:SS)."""
    s = int(round(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def find_font(name: str) -> str:
    """Resolve a font name or partial name to a file path."""
    if os.path.isfile(name):
        return name

    search_dirs = [
        "/System/Library/Fonts/Supplemental",
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
    ]

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if not f.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                if f.lower() == name.lower() or os.path.splitext(f)[0].lower() == name.lower():
                    return os.path.join(root, f)
                if name.lower() in os.path.splitext(f)[0].lower():
                    return os.path.join(root, f)

    return "/System/Library/Fonts/Supplemental/Baskerville.ttc"


def word_wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    """Wrap text so each line fits within max_width pixels."""
    paragraphs = text.split("\n")
    wrapped_lines = []
    for para in paragraphs:
        words = para.split()
        if not words:
            wrapped_lines.append("")
            continue
        current_line = words[0]
        for word in words[1:]:
            test_line = current_line + " " + word
            bbox = draw.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                wrapped_lines.append(current_line)
                current_line = word
        wrapped_lines.append(current_line)
    return "\n".join(wrapped_lines)


def run_command(cmd, log_output=False):
    """Run a shell subprocess and optionally stream stdout/stderr."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1
    )
    output_lines = []
    for line in process.stdout:
        output_lines.append(line)
        if log_output:
            print(line, end="", flush=True)
    process.wait()
    return process.returncode, "".join(output_lines)


def get_video_info(file_path):
    """Retrieve video duration, dimensions, frame rate, and audio properties via ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of", "json",
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)

        duration = float(data.get("format", {}).get("duration", 0.0))
        video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

        width = int(video_stream.get("width", 3840)) if video_stream else 3840
        height = int(video_stream.get("height", 2160)) if video_stream else 2160
        r_fps = video_stream.get("r_frame_rate", "30/1") if video_stream else "30/1"

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": r_fps,
            "has_audio": audio_stream is not None,
            "sample_rate": int(audio_stream.get("sample_rate", 44100)) if audio_stream else 44100,
            "channels": int(audio_stream.get("channels", 2)) if audio_stream else 2
        }
    except Exception as ex:
        print(f"❌ Error probing video {file_path}: {ex}")
        return None


def verify_media_file(file_path, min_duration=1.0):
    """Check if an MP4 file exists, is non-empty, and has a valid duration."""
    if not os.path.isfile(file_path) or os.path.getsize(file_path) < 1024:
        return False
    info = get_video_info(file_path)
    if not info or info["duration"] < min_duration:
        return False
    return True


def normalize_key(s: str) -> str:
    """Strip punctuation and whitespace for robust database matching."""
    return re.sub(r'[^a-zA-Z0-9]', '', s).lower() if s else ''


def load_database_lookup(db_path=DB_PATH):
    """
    Builds a multi-index lookup dictionary mapping folder names, project names,
    and filenames to (official_name, overlay_text).
    """
    lookup_exact = {}
    lookup_norm = {}

    if os.path.isfile(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT id, name, overlay_text, folder_path, video_file FROM projects")
            for row in cursor.fetchall():
                pid, name, overlay, fpath, vfile = row
                name = name.strip() if name else ""
                overlay = overlay.strip() if overlay else ""

                val = (name, overlay)

                if name:
                    lookup_exact[name.lower()] = val
                    lookup_norm[normalize_key(name)] = val

                if fpath:
                    b_folder = os.path.basename(fpath.rstrip('/'))
                    lookup_exact[b_folder.lower()] = val
                    lookup_norm[normalize_key(b_folder)] = val

                if vfile:
                    b_file = os.path.splitext(os.path.basename(vfile))[0]
                    lookup_exact[b_file.lower()] = val
                    lookup_norm[normalize_key(b_file)] = val

            conn.close()
        except Exception:
            pass

    return lookup_exact, lookup_norm


def match_track_data(folder_name, file_name, lookup_exact, lookup_norm):
    """
    Matches a given folder and filename to the official database record.
    Returns (track_title, quote_text).
    """
    clean_f = re.sub(r'\s*4k.*$', '', folder_name, flags=re.IGNORECASE).strip()
    clean_file = re.sub(r'\s*4k.*$', '', os.path.splitext(file_name)[0], flags=re.IGNORECASE).strip()

    # Try exact case-insensitive matches
    for candidate in [folder_name, clean_f, file_name, clean_file]:
        if candidate.lower() in lookup_exact:
            return lookup_exact[candidate.lower()]

    # Try normalized matches
    for candidate in [folder_name, clean_f, file_name, clean_file]:
        norm = normalize_key(candidate)
        if norm in lookup_norm:
            return lookup_norm[norm]

    # Fallback to cleaned folder name
    return clean_f or folder_name, ""


def discover_videos(input_dir):
    """
    Scans the input directory and returns a sorted list of main 4K video files.
    Matches files ending in '4k (Video).mp4' while excluding Shorts.
    """
    if not os.path.isdir(input_dir):
        print(f"❌ Input directory not found: {input_dir}")
        return []

    folder_entries = sorted([
        d for d in os.listdir(input_dir)
        if os.path.isdir(os.path.join(input_dir, d))
        and not d.startswith(".")
        and not d.endswith(".imovielibrary")
    ])

    matched_videos = []

    for folder in folder_entries:
        folder_path = os.path.join(input_dir, folder)
        expected_main = os.path.join(folder_path, f"{folder} 4k (Video).mp4")

        if os.path.isfile(expected_main):
            matched_videos.append({
                "folder": folder,
                "file_path": expected_main,
                "file_name": os.path.basename(expected_main)
            })
        else:
            try:
                candidates = [
                    f for f in os.listdir(folder_path)
                    if f.endswith("4k (Video).mp4")
                    and "short" not in f.lower()
                    and not f.startswith(".")
                ]
                for c in sorted(candidates):
                    matched_videos.append({
                        "folder": folder,
                        "file_path": os.path.join(folder_path, c),
                        "file_name": c
                    })
            except Exception:
                pass

    return matched_videos


def sanitize_filename(name):
    """Generate a clean slug for segment caching."""
    return re.sub(r'[^a-zA-Z0-9_-]', '_', name)


def load_checkpoint(checkpoint_file):
    """Load JSON checkpoint state."""
    if os.path.isfile(checkpoint_file):
        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed_segments": {}, "final_concat": False}


def save_checkpoint(checkpoint_file, state):
    """Save JSON checkpoint state atomically."""
    tmp_path = checkpoint_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, checkpoint_file)


def create_hypnosonica_presentation_png(
    track_title: str,
    series_text: str,
    quote_text: str,
    badge_text: str,
    output_png: str,
    video_w: int = 3840,
    video_h: int = 2160,
    font_name: str = DEFAULT_FONT,
    title_size: int = 100,
    series_size: int = 68,
    quote_size: int = 56,
    badge_size: int = 50,
    pad_x: int = 90,
    pad_y: int = 75,
    max_text_width: int = 2000,
    opacity: float = 0.95
):
    """
    Renders the right-aligned Hypnosonica presentation typography in the top-right corner (2x balanced scale):
      Line 1: Track Title (prominent ~100pt)
      Line 2: Hypnosonica Presents: Music To Sleep To (~68pt)
      Line 3: "[Bedtime Sleep Quote from Database]" (~56pt, word-wrapped if long)
      Line 4: 432Hz Deep Sleep & Relaxation (~50pt Audio / Frequency Badge)
    """
    img = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_path = find_font(font_name)
    try:
        t_font = ImageFont.truetype(font_path, title_size)
    except Exception:
        t_font = ImageFont.load_default()

    try:
        s_font = ImageFont.truetype(font_path, series_size)
    except Exception:
        s_font = ImageFont.load_default()

    try:
        q_font = ImageFont.truetype(font_path, quote_size)
    except Exception:
        q_font = ImageFont.load_default()

    try:
        b_font = ImageFont.truetype(font_path, badge_size)
    except Exception:
        b_font = ImageFont.load_default()

    # Calculate fixed typographic line heights from font metrics for 100% uniform spacing
    try:
        t_m = t_font.getmetrics()
        t_line_h = t_m[0] + t_m[1]
    except Exception:
        t_line_h = int(title_size * 1.15)

    try:
        s_m = s_font.getmetrics()
        s_line_h = s_m[0] + s_m[1]
    except Exception:
        s_line_h = int(series_size * 1.15)

    try:
        q_m = q_font.getmetrics()
        q_line_h = q_m[0] + q_m[1]
    except Exception:
        q_line_h = int(quote_size * 1.15)

    try:
        b_m = b_font.getmetrics()
        b_line_h = b_m[0] + b_m[1]
    except Exception:
        b_line_h = int(badge_size * 1.15)

    curr_y = pad_y

    t_alpha = int(round(255 * opacity))
    s_alpha = int(round(225 * opacity))
    q_alpha = int(round(215 * opacity))
    b_alpha = int(round(195 * opacity))
    shadow_alpha = int(round(220 * opacity))

    # 1. Line 1: Track Title
    if track_title:
        t_box = draw.textbbox((0, 0), track_title, font=t_font)
        t_w = t_box[2] - t_box[0]
        t_x = video_w - pad_x - t_w
        draw.text((t_x + 4, curr_y + 4), track_title, font=t_font, fill=(0, 0, 0, shadow_alpha))
        draw.text((t_x, curr_y), track_title, font=t_font, fill=(255, 255, 255, t_alpha))
        curr_y += t_line_h + 16

    # 2. Line 2: Series / Presenter
    if series_text:
        s_box = draw.textbbox((0, 0), series_text, font=s_font)
        s_w = s_box[2] - s_box[0]
        s_x = video_w - pad_x - s_w
        draw.text((s_x + 3, curr_y + 3), series_text, font=s_font, fill=(0, 0, 0, shadow_alpha))
        draw.text((s_x, curr_y), series_text, font=s_font, fill=(225, 225, 225, s_alpha))
        curr_y += s_line_h + 20

    # 3. Line 3: Bedtime Quote (formatted in quotes, word-wrapped if long)
    if quote_text:
        formatted_quote = f'"{quote_text.strip()}"' if not quote_text.startswith('"') else quote_text.strip()
        wrapped_quote = word_wrap(formatted_quote, q_font, max_text_width, draw)
        for line in wrapped_quote.split("\n"):
            if not line.strip():
                continue
            q_box = draw.textbbox((0, 0), line, font=q_font)
            q_w = q_box[2] - q_box[0]
            q_x = video_w - pad_x - q_w
            draw.text((q_x + 3, curr_y + 3), line, font=q_font, fill=(0, 0, 0, shadow_alpha))
            draw.text((q_x, curr_y), line, font=q_font, fill=(235, 230, 220, q_alpha))
            curr_y += q_line_h + 8
        curr_y += 12

    # 4. Line 4: Audio / Frequency Badge
    if badge_text:
        b_box = draw.textbbox((0, 0), badge_text, font=b_font)
        b_w = b_box[2] - b_box[0]
        b_x = video_w - pad_x - b_w
        draw.text((b_x + 3, curr_y + 3), badge_text, font=b_font, fill=(0, 0, 0, shadow_alpha))
        draw.text((b_x, curr_y), badge_text, font=b_font, fill=(190, 210, 230, b_alpha))

    img.save(output_png)
    return True


def encode_segment(
    input_path,
    output_path,
    cache_dir,
    segment_idx=1,
    track_title="",
    series_text=DEFAULT_PRESENTATION_SERIES,
    quote_text="",
    badge_text=DEFAULT_BADGE_TEXT,
    no_text=False,
    text_display="full",
    font_name=DEFAULT_FONT,
    fade_dur=5.0,
    pad_dur=5.0,
    watermark_path=DEFAULT_WATERMARK,
    watermark_opacity=0.4,
    negative_logo=False,
    encoder="hevc_videotoolbox",
    bitrate="14M",
    crf=22
):
    """
    Encodes an individual video with:
    - 5s Fade-In at start
    - 5s Fade-Out at end
    - 5s Black padding + silence at end
    - Persistent 'Music To Sleep To' logo overlay in bottom-right corner that
      remains visible during fade-outs and black pauses!
    - Right-aligned top-right presentation typography:
        Line 1: Track Title
        Line 2: Hypnosonica Presents: Music To Sleep To
        Line 3: "[Bedtime Sleep Quote from Database]"
        Line 4: 432Hz Deep Sleep & Relaxation (Audio Badge)
    - Standardized resolution: 3840x2160, 30 fps, H.265 / HEVC, 44.1kHz AAC Stereo
    """
    info = get_video_info(input_path)
    if not info:
        return False, "Failed to probe input video stream details"

    duration = info["duration"]
    if duration <= (fade_dur * 2):
        actual_fade = max(0.5, duration / 3.0)
    else:
        actual_fade = fade_dur

    fade_out_start = max(0.0, duration - actual_fade)

    has_logo = watermark_path and os.path.isfile(watermark_path)

    # Standard bottom-right logo coordinates (12% height, 2% padding)
    w_h = int(2160 * 0.12)
    pad_x = int(3840 * 0.02)
    pad_y = int(2160 * 0.02)

    # 1. Prepare Text Overlay PNG if enabled
    text_png_path = None
    if not no_text and (track_title or series_text or quote_text or badge_text):
        text_png_path = os.path.join(cache_dir, f"text_{segment_idx:04d}.png")
        create_hypnosonica_presentation_png(
            track_title=track_title,
            series_text=series_text,
            quote_text=quote_text,
            badge_text=badge_text,
            output_png=text_png_path,
            font_name=font_name
        )

    # Audio Filter: Fade-in, Fade-out, append silence
    a_filter = (
        f"aformat=sample_rates=44100:channel_layouts=stereo,"
        f"afade=t=in:st=0:d={actual_fade:.2f},"
        f"afade=t=out:st={fade_out_start:.2f}:d={actual_fade:.2f},"
        f"apad=pad_dur={pad_dur:.2f}"
    )

    tmp_output = output_path + ".tmp.mp4"
    if os.path.isfile(tmp_output):
        os.remove(tmp_output)

    cmd = ["ffmpeg", "-y", "-i", input_path]
    input_idx = 1

    # Base Background Filter
    bg_filter = (
        f"[0:v]scale=3840:2160:force_original_aspect_ratio=decrease,pad=3840:2160:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps=30,format=yuv420p,"
        f"fade=t=in:st=0:d={actual_fade:.2f},"
        f"fade=t=out:st={fade_out_start:.2f}:d={actual_fade:.2f},"
        f"tpad=stop_mode=add:stop_duration={pad_dur:.2f}:color=black[bg]"
    )

    fc_parts = [bg_filter]
    current_stream = "bg"

    # Logo Input & Filter
    if has_logo:
        cmd.extend(["-loop", "1", "-i", watermark_path])
        logo_input_idx = input_idx
        input_idx += 1

        negate_str = ",negate=negate_alpha=0" if negative_logo else ""
        fc_parts.append(
            f"[{logo_input_idx}:v]scale=-1:{w_h},format=rgba,colorchannelmixer=aa={watermark_opacity:.2f}{negate_str}[logo]"
        )
        fc_parts.append(
            f"[{current_stream}][logo]overlay=main_w-overlay_w-{pad_x}:main_h-overlay_h-{pad_y}:format=auto:shortest=1[v_logo]"
        )
        current_stream = "v_logo"

    # Text Input & Filter
    if text_png_path and os.path.isfile(text_png_path):
        cmd.extend(["-loop", "1", "-i", text_png_path])
        text_input_idx = input_idx
        input_idx += 1

        if text_display == "intro":
            # Fade in with video (0-3s), display until 22s, fade out (22-25s)
            fc_parts.append(
                f"[{text_input_idx}:v]fade=t=in:st=0:d=3:alpha=1,fade=t=out:st=22:d=3:alpha=1[txt]"
            )
        else:  # full track duration
            fc_parts.append(
                f"[{text_input_idx}:v]fade=t=in:st=0:d=3:alpha=1,fade=t=out:st={fade_out_start:.2f}:d={actual_fade:.2f}:alpha=1[txt]"
            )

        fc_parts.append(
            f"[{current_stream}][txt]overlay=0:0:format=auto:shortest=1[v_final]"
        )
        current_stream = "v_final"

    filter_complex_str = ";".join(fc_parts)

    cmd.extend([
        "-filter_complex", filter_complex_str,
        "-map", f"[{current_stream}]",
        "-map", "0:a:0?",
        "-af", a_filter
    ])

    if encoder == "hevc_videotoolbox":
        cmd.extend([
            "-c:v", "hevc_videotoolbox",
            "-b:v", bitrate,
            "-tag:v", "hvc1",
            "-profile:v", "main",
            "-allow_sw", "1"
        ])
    else:  # libx265
        cmd.extend([
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", "medium",
            "-tag:v", "hvc1"
        ])

    cmd.extend([
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        tmp_output
    ])

    code, out = run_command(cmd, log_output=False)
    if code != 0:
        if os.path.isfile(tmp_output):
            os.remove(tmp_output)
        return False, f"ffmpeg error (exit code {code}): {out[-400:]}"

    # Verify output file
    expected_dur = duration + pad_dur
    if not verify_media_file(tmp_output, min_duration=expected_dur * 0.9):
        if os.path.isfile(tmp_output):
            os.remove(tmp_output)
        return False, "Rendered segment verification failed (truncated or unreadable output)."

    os.replace(tmp_output, output_path)
    return True, ""


def concatenate_segments(segment_paths, output_path, cache_dir):
    """
    Concatenates pre-encoded segments using the ffmpeg concat demuxer (lossless stream copy).
    """
    concat_list_path = os.path.join(cache_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for seg in segment_paths:
            escaped_path = seg.replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")

    tmp_final = output_path + ".tmp.mp4"
    if os.path.isfile(tmp_final):
        os.remove(tmp_final)

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        "-movflags", "+faststart",
        tmp_final
    ]

    print("\n📦 Performing final seamless concatenation of all segments...")
    code, out = run_command(cmd, log_output=False)
    if code != 0:
        if os.path.isfile(tmp_final):
            os.remove(tmp_final)
        return False, f"Concatenation error (exit code {code}): {out[-400:]}"

    if not verify_media_file(tmp_final, min_duration=10.0):
        if os.path.isfile(tmp_final):
            os.remove(tmp_final)
        return False, "Final stitched video verification failed."

    os.replace(tmp_final, output_path)
    return True, ""


def build_youtube_chapters(videos, lookup_exact, lookup_norm, black_dur=5.0):
    """
    Calculates exact start times for each video and builds YouTube chapter timestamps.
    """
    chapters = []
    current_time = 0.0

    for idx, vid in enumerate(videos, 1):
        info = get_video_info(vid["file_path"])
        dur = info["duration"] if info else 0.0
        title, quote = match_track_data(vid["folder"], vid["file_name"], lookup_exact, lookup_norm)
        ts = format_timestamp(current_time)

        chapters.append({
            "index": idx,
            "track_title": title,
            "quote": quote,
            "start_seconds": current_time,
            "timestamp": ts,
            "duration": dur,
            "folder": vid["folder"],
            "file_path": vid["file_path"]
        })

        current_time += (dur + black_dur)

    return chapters, current_time


def export_youtube_chapters_files(chapters, output_video_path):
    """
    Exports chapters to `[output]_chapters.txt` and `[output]_chapters.json`.
    """
    base_name = os.path.splitext(output_video_path)[0]
    txt_path = f"{base_name}_chapters.txt"
    json_path = f"{base_name}_chapters.json"

    # 1. Text file formatted for YouTube description
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("⏱️ TRACKLIST & YOUTUBE CHAPTERS:\n")
        f.write("----------------------------------------\n")
        for ch in chapters:
            f.write(f"{ch['timestamp']} - {ch['track_title']}\n")
        f.write("----------------------------------------\n")
        f.write("\n\n📖 TRACKLIST WITH SLEEP QUOTES:\n")
        f.write("----------------------------------------\n")
        for ch in chapters:
            quote_str = f" — \"{ch['quote']}\"" if ch['quote'] else ""
            f.write(f"{ch['timestamp']} {ch['track_title']}{quote_str}\n")

    # 2. JSON structured data
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(chapters, f, indent=2)

    return txt_path, json_path


def main():
    parser = argparse.ArgumentParser(
        description="Stitch all 4K videos from '/Volumes/Music To Sleep To' into a master H.265 compilation with presentation text, logo & YouTube chapters."
    )
    parser.add_argument("--input-dir", type=str, default=DEFAULT_INPUT_DIR, help="Source folder containing project subdirectories.")
    parser.add_argument("--output", type=str, default=os.path.join(DEFAULT_OUTPUT_DIR, DEFAULT_OUTPUT_FILENAME), help="Path to save the stitched output MP4.")
    parser.add_argument("--cache-dir", type=str, default=None, help="Directory to store intermediate encoded segments.")
    parser.add_argument("--fade-duration", type=float, default=5.0, help="Fade-in and Fade-out duration in seconds.")
    parser.add_argument("--black-duration", type=float, default=5.0, help="Black gap duration between videos in seconds.")
    parser.add_argument("--watermark", type=str, default=DEFAULT_WATERMARK, help="Path to transparent PNG watermark (default: music_to_sleep_to_profile.png).")
    parser.add_argument("--watermark-opacity", type=float, default=0.4, help="Watermark opacity from 0.0 to 1.0 (default: 0.4).")
    parser.add_argument("--negative-logo", action="store_true", help="Invert watermark colors (negative).")
    parser.add_argument("--no-watermark", action="store_true", help="Disable watermark overlay.")
    parser.add_argument("--series-text", type=str, default=DEFAULT_PRESENTATION_SERIES, help="Line 2 subtitle (default: 'Hypnosonica Presents: Music To Sleep To').")
    parser.add_argument("--badge-text", type=str, default=DEFAULT_BADGE_TEXT, help="Line 4 badge text (default: '432Hz Deep Sleep & Relaxation').")
    parser.add_argument("--no-text", action="store_true", help="Disable text overlay completely.")
    parser.add_argument("--text-display", choices=["intro", "full"], default="full", help="Text display timing: 'full' (entire track until fade-out) or 'intro' (first 25s of each track).")
    parser.add_argument("--font", type=str, default=DEFAULT_FONT, help="Serif font name (default: Baskerville).")
    parser.add_argument("--encoder", choices=["hevc_videotoolbox", "libx265"], default="hevc_videotoolbox", help="HEVC encoder (default: hevc_videotoolbox hardware).")
    parser.add_argument("--bitrate", type=str, default="14M", help="Video bitrate for hevc_videotoolbox (e.g. 14M).")
    parser.add_argument("--crf", type=int, default=22, help="CRF quality for libx265 (default: 22).")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of videos to process (for testing).")
    parser.add_argument("--shuffle", "--randomize", dest="shuffle", action="store_true", help="Randomize / shuffle the order of songs before stitching.")
    parser.add_argument("--seed", type=int, default=None, help="Optional integer seed for reproducible randomized order (e.g. --seed 42).")
    parser.add_argument("--chapters-only", action="store_true", help="Calculate and export YouTube chapters bookmarks without encoding.")
    parser.add_argument("--dry-run", action="store_true", help="Preview discovered files, timestamps, and calculated durations.")
    parser.add_argument("--clean-cache", action="store_true", help="Clear intermediate segment cache before running.")

    args = parser.parse_args()

    # Determine Cache Directory
    if args.cache_dir:
        cache_dir = os.path.abspath(args.cache_dir)
    elif os.path.isdir(args.input_dir) and os.access(args.input_dir, os.W_OK):
        cache_dir = DEFAULT_CACHE_DIR_EXTERNAL
    else:
        cache_dir = DEFAULT_CACHE_DIR_LOCAL

    os.makedirs(cache_dir, exist_ok=True)
    checkpoint_file = os.path.join(cache_dir, "checkpoint.json")

    watermark_file = "" if args.no_watermark else os.path.abspath(args.watermark)
    if watermark_file and not os.path.isfile(watermark_file):
        print(f"⚠️ Watermark file not found at: {watermark_file}. Proceeding without logo.")
        watermark_file = ""

    if args.clean_cache:
        print(f"🧹 Cleaning cache directory: {cache_dir}")
        for item in os.listdir(cache_dir):
            item_path = os.path.join(cache_dir, item)
            if os.path.isfile(item_path):
                os.remove(item_path)

    state = load_checkpoint(checkpoint_file)
    lookup_exact, lookup_norm = load_database_lookup()

    print("=================================================================")
    print(" 🎬 4K Video Stitcher & Presentation Pipeline")
    print(f" 📂 Input Directory:      {args.input_dir}")
    print(f" 💾 Output Destination:   {args.output}")
    print(f" 🗄️  Intermediate Cache:   {cache_dir}")
    print(f" 🎨 Logo Watermark:       {('Enabled (' + os.path.basename(watermark_file) + ' @ ' + str(int(args.watermark_opacity*100)) + '% opacity, persists on fade)') if watermark_file else 'Disabled'}")
    if not args.no_text:
        print(f" ✍️  Text Typography:     Font: {args.font} | Timing: {args.text_display}")
        print(f"    Line 1 (Title):      [Song Title from DB / Filename]")
        print(f"    Line 2 (Series):     {args.series_text}")
        print(f"    Line 3 (Quote):      \"[Bedtime Sleep Quote from DB]\"")
        print(f"    Line 4 (Badge):      {args.badge_text}")
    else:
        print(" ✍️  Text Overlay:        Disabled")
    print(f" ⚙️  Encoder:             {args.encoder} ({args.bitrate if args.encoder == 'hevc_videotoolbox' else f'CRF {args.crf}'})")
    print(f" ⏱️  Transitions:         {args.fade_duration}s fade-in/out | {args.black_duration}s black gap")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No encoding)")
    if args.chapters_only:
        print(" 📖 MODE:                 Chapters Export Only (No encoding)")
    if args.limit:
        print(f" 🔢 LIMIT:                Processing first {args.limit} video(s)")
    print("=================================================================")

    # 1. Discover Videos
    videos = discover_videos(args.input_dir)
    if not videos:
        print("❌ No matching 4k (Video).mp4 files found.")
        sys.exit(1)

    if args.shuffle:
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        rng.shuffle(videos)
        print(f" 🔀 Shuffled video playlist randomly (seed: {args.seed if args.seed is not None else 'dynamic'}).")

    if args.limit:
        videos = videos[:args.limit]

    total_count = len(videos)
    print(f"\n🔍 Found {total_count} main 4K video(s) to compile.")

    # 2. Calculate Chapter Timestamps
    chapters, total_est_duration = build_youtube_chapters(videos, lookup_exact, lookup_norm, args.black_duration)
    txt_path, json_path = export_youtube_chapters_files(chapters, args.output)

    # 3. Preview / Dry-run or Chapters-Only Output
    if args.dry_run or args.chapters_only:
        print("\n--- YouTube Chapter Bookmarks & Presentation Preview ---")
        for ch in chapters:
            quote_snippet = f" | \"{ch['quote'][:35]}...\"" if ch['quote'] else ""
            print(f"  {ch['timestamp']} - {ch['track_title']}{quote_snippet}")

        total_hours = total_est_duration / 3600.0
        print(f"\n📊 Total Master Duration: {total_est_duration:.1f}s (~{total_hours:.2f} hours)")
        print(f"📄 YouTube Chapters saved to: {txt_path}")
        print(f"📁 JSON metadata saved to:    {json_path}")
        if args.chapters_only or args.dry_run:
            return

    # 4. Process & Cache Intermediate Segments
    segment_paths = []
    start_time = time.time()

    for idx, vid in enumerate(videos, 1):
        if INTERRUPTED:
            break

        folder_name = vid["folder"]
        src_path = vid["file_path"]
        safe_name = sanitize_filename(f"seg_{folder_name}")
        seg_output = os.path.join(cache_dir, f"{safe_name}.mp4")
        segment_paths.append(seg_output)

        # Check if already completed and verified
        cached_info = state.get("completed_segments", {}).get(src_path)
        if (cached_info or os.path.isfile(seg_output)) and verify_media_file(seg_output):
            print(f"  ⏩ [{idx:3d}/{total_count}] [Cached] {folder_name}")
            continue

        track_title, quote_text = match_track_data(folder_name, vid["file_name"], lookup_exact, lookup_norm)
        ch_meta = next((c for c in chapters if c["index"] == idx), None)
        timestamp_str = ch_meta["timestamp"] if ch_meta else "00:00:00"

        print(f"\n  ⏳ [{idx:3d}/{total_count}] ({timestamp_str}) Encoding: \"{track_title}\"...")
        if quote_text:
            print(f"     💬 Quote: \"{quote_text}\"")
        seg_start = time.time()

        ok, err = encode_segment(
            input_path=src_path,
            output_path=seg_output,
            cache_dir=cache_dir,
            segment_idx=idx,
            track_title=track_title,
            series_text=args.series_text,
            quote_text=quote_text,
            badge_text=args.badge_text,
            no_text=args.no_text,
            text_display=args.text_display,
            font_name=args.font,
            fade_dur=args.fade_duration,
            pad_dur=args.black_duration,
            watermark_path=watermark_file,
            watermark_opacity=args.watermark_opacity,
            negative_logo=args.negative_logo,
            encoder=args.encoder,
            bitrate=args.bitrate,
            crf=args.crf
        )

        if not ok:
            print(f"     ❌ Failed to encode \"{folder_name}\": {err}")
            sys.exit(1)

        seg_elapsed = time.time() - seg_start
        print(f"     ✨ Finished in {seg_elapsed:.1f}s -> {os.path.basename(seg_output)}")

        # Update checkpoint
        state.setdefault("completed_segments", {})[src_path] = {
            "output_file": seg_output,
            "timestamp": time.time()
        }
        save_checkpoint(checkpoint_file, state)

    if INTERRUPTED:
        print("🛑 Encoding halted. Re-run script to resume from where it stopped.")
        sys.exit(0)

    # 5. Final Master Concatenation
    output_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(output_dir, exist_ok=True)

    concat_ok, concat_err = concatenate_segments(segment_paths, args.output, cache_dir)
    if not concat_ok:
        print(f"❌ Final concatenation failed: {concat_err}")
        sys.exit(1)

    state["final_concat"] = True
    save_checkpoint(checkpoint_file, state)

    total_time = time.time() - start_time
    file_size_gb = os.path.getsize(args.output) / (1024 ** 3)
    info = get_video_info(args.output)
    final_dur_h = (info["duration"] / 3600) if info else 0.0

    print("\n=================================================================")
    print(" 🎉 ALL VIDEOS STITCHED SUCCESSFULLY!")
    print(f" 📁 Master File:      {args.output}")
    print(f" 💾 File Size:        {file_size_gb:.2f} GB")
    print(f" ⏱️  Duration:         {final_dur_h:.2f} hours")
    print(f" 📄 YouTube Chapters: {txt_path}")
    print(f" ⚡ Total Processing Time: {total_time / 60:.1f} minutes")
    print("=================================================================")


if __name__ == "__main__":
    main()
