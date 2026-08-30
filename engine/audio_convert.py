#!/usr/bin/env python3
"""
Audio Convert Tool

Convert audio files between formats using ffmpeg.

Supported formats: flac, mp3, wav, aac, ogg, m4a, wma, aiff, alac

Usage:
    python audio_convert.py -i song.mp3 -o song.flac
    python audio_convert.py -i song.mp3 -o song.wav
    python audio_convert.py -i song.flac -o song.mp3 --bitrate 320k
    python audio_convert.py -i song.wav -o song.ogg --bitrate 256k
    python audio_convert.py -i song.mp3 --format flac
    python audio_convert.py -i *.mp3 --format flac

Dependencies:
    ffmpeg must be installed (brew install ffmpeg)
"""

import argparse
import os
import shutil
import subprocess
import sys


CODECS = {
    ".flac": ["-c:a", "flac"],
    ".mp3":  ["-c:a", "libmp3lame"],
    ".wav":  ["-c:a", "pcm_s16le"],
    ".aac":  ["-c:a", "aac"],
    ".m4a":  ["-c:a", "aac"],
    ".ogg":  ["-c:a", "libvorbis"],
    ".wma":  ["-c:a", "wmav2"],
    ".aiff": ["-c:a", "pcm_s16be"],
    ".alac": ["-c:a", "alac"],
}

LOSSLESS = {".flac", ".wav", ".aiff", ".alac"}


def convert_audio(input_path: str, output_path: str,
                  bitrate: str | None = None) -> None:
    """
    Convert an audio file to a different format via ffmpeg.

    Parameters
    ----------
    input_path : str
        Source audio file.
    output_path : str
        Destination file — the extension determines the codec.
    bitrate : str, optional
        Bitrate for lossy formats (e.g. "320k", "256k").
        Ignored for lossless formats.
    """
    ext = os.path.splitext(output_path)[1].lower()
    codec_flags = CODECS.get(ext)
    if codec_flags is None:
        sys.exit(f"Error: unsupported output format '{ext}'. "
                 f"Supported: {', '.join(sorted(CODECS))}")

    cmd = ["ffmpeg", "-y", "-i", input_path] + codec_flags

    if bitrate and ext not in LOSSLESS:
        cmd += ["-b:a", bitrate]

    cmd.append(output_path)

    print(f"  {os.path.basename(input_path)} → {os.path.basename(output_path)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ffmpeg stderr:\n{result.stderr}")
        sys.exit(f"Error: ffmpeg failed (exit code {result.returncode})")

    in_mb = os.path.getsize(input_path) / (1024 * 1024)
    out_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Done: {in_mb:.1f} MB → {out_mb:.1f} MB")


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("Error: ffmpeg not found. Install it with: brew install ffmpeg")

    parser = argparse.ArgumentParser(
        description="Convert audio files between formats using ffmpeg."
    )
    parser.add_argument(
        "--input", "-i", nargs="+", required=True,
        help="Input audio file(s).",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Output file (for single input). Extension determines format.",
    )
    parser.add_argument(
        "--format", "-f", default=None,
        metavar="EXT",
        help="Target format when converting multiple files (e.g. flac, mp3, wav). "
             "Output files are saved alongside the originals.",
    )
    parser.add_argument(
        "--bitrate", "-b", default=None,
        metavar="RATE",
        help="Bitrate for lossy formats (e.g. 320k, 256k, 192k). "
             "Ignored for lossless formats like flac/wav.",
    )
    parser.add_argument(
        "--output-dir", "-d", default=None,
        metavar="DIR",
        help="Save converted files to this directory.",
    )

    args = parser.parse_args()

    if len(args.input) == 1 and args.output:
        # Single file with explicit output
        if not os.path.isfile(args.input[0]):
            sys.exit(f"Error: file not found: {args.input[0]}")
        convert_audio(args.input[0], args.output, bitrate=args.bitrate)
    elif args.format:
        # Batch: convert all inputs to the target format
        fmt = args.format.lstrip(".")
        ext = f".{fmt}"
        if ext not in CODECS:
            sys.exit(f"Error: unsupported format '{fmt}'. "
                     f"Supported: {', '.join(e.lstrip('.') for e in sorted(CODECS))}")

        out_dir = args.output_dir
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        print(f"Converting {len(args.input)} file(s) → {fmt}")
        for path in args.input:
            if not os.path.isfile(path):
                print(f"  Warning: not found, skipping: {path}")
                continue
            base = os.path.splitext(os.path.basename(path))[0]
            if out_dir:
                dst = os.path.join(out_dir, base + ext)
            else:
                dst = os.path.join(os.path.dirname(path), base + ext)
            convert_audio(path, dst, bitrate=args.bitrate)
    else:
        sys.exit("Error: provide --output/-o for a single file, "
                 "or --format/-f for batch conversion.")


if __name__ == "__main__":
    main()
