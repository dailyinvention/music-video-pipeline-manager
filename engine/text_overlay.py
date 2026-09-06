#!/usr/bin/env python3
"""
Text Overlay Tool

Add text to a video with automatic sizing, font selection, and fade control.

Renders the text as a transparent PNG with Pillow, then composites it onto
the video using ffmpeg's overlay filter — no drawtext/libfreetype required.

Usage:
    python text_overlay.py -i video.mp4 -o titled.mp4 --text "Hello World"
    python text_overlay.py -i video.mp4 -o out.mp4 --text "Title" --font /path/to/font.ttf
    python text_overlay.py -i video.mp4 -o out.mp4 --text "Intro" --fade-in 0 2 --fade-out 8 10
    python text_overlay.py -i video.mp4 -o out.mp4 --text "Bottom" --position bottom --padding 0.05
    python text_overlay.py -i video.mp4 -o out.mp4 --text "Line 1" --text "Line 2"
    python text_overlay.py -i video.mp4 -o out.mp4 --text "Yellow" --color yellow

Dependencies:
    pip install Pillow
    ffmpeg must be installed (brew install ffmpeg)
"""

import argparse
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------

def find_font(name: str) -> str:
    """Resolve a font name or partial name to a file path."""
    if os.path.isfile(name):
        return name

    search_dirs = [
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
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

    return name  # let ffmpeg try as-is


# ---------------------------------------------------------------------------
# Probe video dimensions
# ---------------------------------------------------------------------------

def probe_video(path: str) -> tuple[int, int, float]:
    """Return (width, height, duration) of a video file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffprobe failed: {result.stderr}")

    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    # First line: width,height  Second line: duration
    w, h = 0, 0
    duration = 0.0
    for line in lines:
        parts = line.split(",")
        if len(parts) == 2:
            try:
                a, b = int(parts[0]), int(parts[1])
                w, h = a, b
                continue
            except ValueError:
                pass
        try:
            duration = float(parts[0])
        except ValueError:
            pass

    if w == 0 or h == 0:
        sys.exit("Error: could not determine video dimensions")
    if duration <= 0:
        # fallback duration probe
        cmd2 = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        try:
            duration = float(r2.stdout.strip())
        except ValueError:
            duration = 0.0

    return w, h, duration


# ---------------------------------------------------------------------------
# Text rendering (Pillow → PNG)
# ---------------------------------------------------------------------------

def word_wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int,
              draw: ImageDraw.ImageDraw) -> str:
    """
    Wrap text so each line fits within max_width pixels.
    Preserves existing newlines from the input.
    """
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


def fit_font_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_width: int,
    max_height: int,
) -> tuple[ImageFont.FreeTypeFont, str]:
    """
    Binary-search for the largest font size where the word-wrapped text
    fits within max_width × max_height. Returns (font, wrapped_text).
    """
    lo, hi = 10, 800
    best = lo
    best_wrapped = text
    while lo <= hi:
        mid = (lo + hi) // 2
        font = ImageFont.truetype(font_path, mid)
        wrapped = word_wrap(text, font, max_width, draw)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= max_width and th <= max_height:
            best = mid
            best_wrapped = wrapped
            lo = mid + 1
        else:
            hi = mid - 1
    return ImageFont.truetype(font_path, best), best_wrapped


def render_text_png(
    text: str,
    video_w: int,
    video_h: int,
    font_path: str,
    padding: float,
    color: tuple,
    position: str,
    shadow: bool,
    shadow_color: tuple,
    png_path: str,
    fontsize: int | None = None,
):
    """Render text onto a transparent PNG matching the video dimensions."""
    img = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad_x = int(video_w * padding)
    pad_y = int(video_h * padding)
    max_w = video_w - 2 * pad_x
    max_h = video_h - 2 * pad_y

    if fontsize is not None:
        # Manual font size — just wrap to fit width
        font = ImageFont.truetype(font_path, fontsize)
        wrapped = word_wrap(text, font, max_w, draw)
    else:
        # Auto-size: find the largest font that fits with wrapping
        font, wrapped = fit_font_size(draw, text, font_path, max_w, max_h)
        fontsize = font.size

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (video_w - tw) // 2
    if position == "top":
        y = pad_y
    elif position == "bottom":
        y = video_h - pad_y - th
    else:
        y = (video_h - th) // 2

    if shadow:
        offset = max(3, fontsize // 15)
        draw.multiline_text(
            (x + offset, y + offset), wrapped, font=font,
            fill=(*shadow_color, 230), align="center",
        )

    draw.multiline_text(
        (x, y), wrapped, font=font,
        fill=(*color, 255), align="center",
    )

    img.save(png_path)
    print(f"  Text PNG: {video_w}x{video_h}, fontsize={fontsize}")
    if wrapped != text:
        print(f"  Wrapped:  {wrapped}")


# ---------------------------------------------------------------------------
# Build ffmpeg overlay filter with fade
# ---------------------------------------------------------------------------

def build_overlay_filter(
    fade_in: tuple | None,
    fade_out: tuple | None,
    duration: float,
) -> str:
    """
    Build an ffmpeg filter_complex that overlays a PNG (input 1) onto
    the video (input 0) with optional fade-in and fade-out on the overlay.
    Uses ffmpeg's native fade filter on the overlay alpha channel.
    """
    # Chain of filters to apply to the overlay PNG before compositing
    overlay_chain = ["format=rgba"]

    if fade_in:
        fi_start, fi_end = fade_in
        fi_dur = fi_end - fi_start
        overlay_chain.append(f"fade=t=in:st={fi_start}:d={fi_dur}:alpha=1")

    if fade_out:
        fo_start, fo_end = fade_out
        fo_dur = fo_end - fo_start
        overlay_chain.append(f"fade=t=out:st={fo_start}:d={fo_dur}:alpha=1")

    overlay_filters = ",".join(overlay_chain)
    fc = (
        f"[1:v]{overlay_filters}[txt];"
        f"[0:v][txt]overlay=0:0:format=auto:shortest=1[vout]"
    )
    return fc


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def overlay_text(
    input_path: str,
    output_path: str,
    png_path: str,
    fade_in: tuple | None,
    fade_out: tuple | None,
    duration: float,
    codec: str,
    crf: int,
):
    import sys
    if codec.lower() in ("h265", "hevc"):
        vcodec = "hevc_videotoolbox" if sys.platform == "darwin" else "libx265"
    else:
        vcodec = "h264_videotoolbox" if sys.platform == "darwin" else "libx264"

    fc = build_overlay_filter(fade_in, fade_out, duration)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-loop", "1", "-i", png_path,
        "-filter_complex", fc,
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", vcodec,
    ]
    if "videotoolbox" in vcodec:
        q_val = max(1, min(100, int(round(115 - 2.5 * crf))))
        cmd += ["-q:v", str(q_val)]
    else:
        cmd += ["-crf", str(crf), "-preset", "slow"]

    cmd += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "320k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]

    print("Running ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg stderr:\n{result.stderr}")
        sys.exit(f"Error: ffmpeg failed (exit code {result.returncode})")


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------

def parse_color(s: str) -> tuple:
    """Parse a color string to an (R, G, B) tuple."""
    named = {
        "white": (255, 255, 255), "black": (0, 0, 0),
        "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
        "yellow": (255, 255, 0), "cyan": (0, 255, 255),
        "magenta": (255, 0, 255), "orange": (255, 165, 0),
        "pink": (255, 192, 203), "purple": (128, 0, 128),
        "gray": (128, 128, 128), "grey": (128, 128, 128),
    }
    s = s.strip().lower()
    if s in named:
        return named[s]
    if s.startswith("#") and len(s) == 7:
        return (int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))
    if "," in s:
        parts = [int(x.strip()) for x in s.split(",")]
        if len(parts) == 3:
            return tuple(parts)
    sys.exit(f"Error: invalid color '{s}'. Use a name (white), hex (#ff0000), or R,G,B.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Add text overlay to a video with auto-sizing, font selection, and fade control."
    )
    parser.add_argument("--input", "-i", required=True, help="Input video file.")
    parser.add_argument("--output", "-o", required=True, help="Output video file.")
    parser.add_argument(
        "--text", "-t", action="append", required=True,
        help="Text to display. Use multiple --text flags for multiple lines.",
    )
    parser.add_argument(
        "--font", "-f", default="Helvetica",
        help="Font name or path to .ttf/.otf file (default: Helvetica).",
    )
    parser.add_argument(
        "--fontsize", type=int, default=None,
        help="Font size in pixels. If omitted, auto-sized to fit the video. "
             "Text wraps automatically when a line exceeds the padded width.",
    )
    parser.add_argument(
        "--padding", type=float, default=0.1,
        help="Padding as fraction of video size (default: 0.1 = 10%%).",
    )
    parser.add_argument(
        "--color", default="white",
        help="Text color: name (white), hex (#ff0000), or R,G,B. Default: white.",
    )
    parser.add_argument(
        "--position", choices=["top", "center", "bottom"], default="center",
        help="Vertical position (default: center).",
    )
    parser.add_argument(
        "--no-shadow", action="store_true",
        help="Disable drop shadow.",
    )
    parser.add_argument(
        "--shadow-color", default="black",
        help="Shadow color: name (black), hex (#ffffff), or R,G,B. Default: black.",
    )
    parser.add_argument(
        "--fade-in", nargs=2, type=float, metavar=("START", "END"),
        help="Fade in from START to END seconds (e.g. --fade-in 0 2).",
    )
    parser.add_argument(
        "--fade-out", nargs=2, type=float, metavar=("START", "END"),
        help="Fade out from START to END seconds (e.g. --fade-out 8 10).",
    )
    parser.add_argument(
        "--codec", choices=["h264", "h265"], default="h265",
        help="Output codec (default: h265). Uses Apple Silicon hardware acceleration on macOS.",
    )
    parser.add_argument(
        "--crf", type=int, default=18,
        help="Output quality (default: 18).",
    )

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    font_path = find_font(args.font)
    try:
        ImageFont.truetype(font_path, 20)
    except OSError:
        sys.exit(
            f"Error: cannot load font '{args.font}' (resolved to '{font_path}').\n"
            f"Provide a path to a .ttf/.otf file or a system font name."
        )

    color = parse_color(args.color)
    shadow_color = parse_color(args.shadow_color)
    fade_in = tuple(args.fade_in) if args.fade_in else None
    fade_out = tuple(args.fade_out) if args.fade_out else None

    # Join multiple --text args into newline-separated string
    text = "\n".join(args.text)

    print(f"Probing video: {args.input}")
    video_w, video_h, duration = probe_video(args.input)
    print(f"  {video_w}x{video_h}, {duration:.2f}s")

    print(f"Text:     {text}")
    print(f"Font:     {font_path}")
    print(f"Position: {args.position}")
    if fade_in:
        print(f"Fade in:  {fade_in[0]:.1f}s → {fade_in[1]:.1f}s")
    if fade_out:
        print(f"Fade out: {fade_out[0]:.1f}s → {fade_out[1]:.1f}s")

    # Render text to a temporary PNG
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        render_text_png(
            text, video_w, video_h, font_path,
            padding=args.padding,
            color=color,
            position=args.position,
            shadow=not args.no_shadow,
            shadow_color=shadow_color,
            png_path=tmp.name,
            fontsize=args.fontsize,
        )

        overlay_text(
            args.input, args.output, tmp.name,
            fade_in, fade_out, duration,
            args.codec, args.crf,
        )
    finally:
        os.unlink(tmp.name)

    out_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"\nDone! {args.output} ({out_mb:.1f} MB)")


if __name__ == "__main__":
    main()
