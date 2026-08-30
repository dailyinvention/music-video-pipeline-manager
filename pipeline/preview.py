"""
Preview system for the Music Video Pipeline Manager.

Handles:
  - Creating a low-res proxy video with merged audio
  - Running loop_finder to get candidate loop points
  - Exporting each candidate as a small playable clip
  - Embedded video player widget for tkinter
"""

import json
import os
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageTk

# Add parent dir so we can import loop_finder
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from pipeline import get_binary_path


PROXY_HEIGHT = 480  # px – height of the preview proxy


# ---------------------------------------------------------------------------
# Proxy + loop analysis
# ---------------------------------------------------------------------------

def create_proxy(video_path: str, audio_path: str,
                 output_path: str, height: int = PROXY_HEIGHT) -> bool:
    """
    Create a small proxy video (with merged audio if available).
    Returns True on success.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    import sys
    if sys.platform == "darwin":
        codec_args = ["-c:v", "h264_videotoolbox", "-q:v", "50"]
    else:
        codec_args = ["-c:v", "libx264", "-crf", "28", "-preset", "ultrafast"]

    has_audio = audio_path and os.path.isfile(audio_path)

    if has_audio:
        cmd = [
            get_binary_path("ffmpeg"), "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex", "[1:a]apad[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
        ] + codec_args + [
            "-vf", f"scale=-2:{height}",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        cmd = [
            get_binary_path("ffmpeg"), "-y",
            "-i", video_path,
        ] + codec_args + [
            "-vf", f"scale=-2:{height}",
            "-an",
            "-movflags", "+faststart",
            output_path,
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def analyze_loops(proxy_path: str, n_candidates: int = 5,
                  min_duration: float = 10.0,
                  max_duration: float | None = None) -> list[dict]:
    """
    Run loop_finder analysis on the proxy video.
    Returns a list of candidate dicts with keys:
      start_time, end_time, duration, score, start_frame, end_frame
    """
    import loop_finder

    y, sr = loop_finder.extract_audio(proxy_path)
    # Detect fps
    cap = cv2.VideoCapture(proxy_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()

    candidates = loop_finder.find_loop_points(
        y, sr,
        min_duration=min_duration,
        max_duration=max_duration,
        n_candidates=n_candidates,
        fps=fps,
    )
    return candidates


def export_preview_clip(proxy_path: str, candidate: dict,
                        output_path: str, fade: float = 2.0) -> bool:
    """
    Export a single loop candidate as a playable clip from the proxy.
    Returns True on success.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    start = candidate["start_time"]
    end = candidate["end_time"]
    dur = candidate["duration"]

    # Build fade filters if duration allows
    vf_parts = []
    af_parts = []
    if fade > 0 and fade * 2 < dur:
        vf_parts.append(f"fade=t=in:st=0:d={fade}")
        vf_parts.append(f"fade=t=out:st={dur - fade}:d={fade}")
        af_parts.append(f"afade=t=in:st=0:d={fade}")
        af_parts.append(f"afade=t=out:st={dur - fade}:d={fade}")

    cmd = [
        get_binary_path("ffmpeg"), "-y",
        "-i", proxy_path,
        "-ss", str(start), "-t", str(end - start),
    ]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]

    import sys
    if sys.platform == "darwin":
        codec_args = ["-c:v", "h264_videotoolbox", "-q:v", "55"]
    else:
        codec_args = ["-c:v", "libx264", "-crf", "26", "-preset", "fast"]

    cmd += codec_args + [
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def generate_all_previews(video_path: str, audio_path: str,
                          project_folder: str,
                          n_candidates: int = 5,
                          force: bool = False,
                          min_duration: float = 10.0,
                          max_duration: float | None = None,
                          progress_callback=None) -> tuple[str, list[dict]]:
    """
    Full preview pipeline:
      1. Create proxy video
      2. Analyze loops
      3. Export each candidate as a clip

    Returns (proxy_path, candidates_with_preview_paths).
    progress_callback(message, pct) is called with updates.
    """
    preview_dir = os.path.join(project_folder, ".previews")
    os.makedirs(preview_dir, exist_ok=True)

    proxy_path = os.path.join(preview_dir, "proxy.mp4")

    # Step 1: proxy
    if progress_callback:
        progress_callback("Creating preview proxy…", 10)
    if force or not os.path.isfile(proxy_path):
        if os.path.isfile(proxy_path):
            try:
                os.remove(proxy_path)
            except Exception:
                pass
        ok = create_proxy(video_path, audio_path, proxy_path)
        if not ok:
            raise RuntimeError("Failed to create proxy video")

    # Step 2: analyze
    if progress_callback:
        progress_callback("Analyzing loop points…", 40)
    candidates = analyze_loops(
        proxy_path,
        n_candidates=n_candidates,
        min_duration=min_duration,
        max_duration=max_duration
    )

    # Step 3: export clips
    total = len(candidates)
    for i, c in enumerate(candidates):
        clip_path = os.path.join(preview_dir, f"loop_{i + 1}.mp4")
        if progress_callback:
            pct = 50 + int(45 * (i / max(total, 1)))
            progress_callback(f"Exporting loop {i + 1}/{total}…", pct)
        if force or not os.path.isfile(clip_path):
            if os.path.isfile(clip_path):
                try:
                    os.remove(clip_path)
                except Exception:
                    pass
            export_preview_clip(proxy_path, c, clip_path, fade=0.0)
        c["preview_path"] = clip_path

    if progress_callback:
        progress_callback("Done", 100)

    return proxy_path, candidates


def analyze_canvas(proxy_path: str, n_candidates: int = 5, duration: float = 2.5) -> list[dict]:
    """Find visually appealing 2.5-second chunks using frame differences."""
    cap = cv2.VideoCapture(proxy_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    
    frame_diffs = []
    ret, prev_frame = cap.read()
    if not ret:
        return []
    prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        diff = cv2.absdiff(gray, prev_gray)
        frame_diffs.append(np.mean(diff))
        prev_gray = gray
        
    cap.release()
    
    if not frame_diffs:
        return []
        
    window_size = int(duration * fps)
    if window_size >= len(frame_diffs):
        return [{"start_time": 0.0, "end_time": len(frame_diffs)/fps, "duration": len(frame_diffs)/fps, "score": 0.0}]
        
    window = np.ones(window_size)
    scores = np.convolve(frame_diffs, window, mode='valid')
    
    video_duration = len(frame_diffs) / fps
    max_separation = video_duration / (n_candidates + 1)
    separation_seconds = min(15.0, max(duration, max_separation))
    separation_frames = int(separation_seconds * fps)

    candidates = []
    scores_copy = scores.copy()
    for _ in range(n_candidates):
        if np.max(scores_copy) <= 0.0:
            break
        max_idx = np.argmax(scores_copy)
        max_score = scores_copy[max_idx]
        
        start_time = max_idx / fps
        candidates.append({
            "start_time": start_time,
            "end_time": start_time + duration,
            "duration": duration,
            "score": float(max_score)
        })
        
        clear_start = max(0, max_idx - separation_frames)
        clear_end = min(len(scores_copy), max_idx + separation_frames)
        scores_copy[clear_start:clear_end] = 0.0
        
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


def generate_canvas_previews(video_path: str, audio_path: str,
                             project_folder: str,
                             n_candidates: int = 5,
                             force: bool = False,
                             progress_callback=None) -> tuple[str, list[dict]]:
    """Generate Spotify Canvas previews."""
    preview_dir = os.path.join(project_folder, ".previews")
    os.makedirs(preview_dir, exist_ok=True)
    proxy_path = os.path.join(preview_dir, "proxy.mp4")

    if progress_callback:
        progress_callback("Creating preview proxy…", 10)
    if force or not os.path.isfile(proxy_path):
        if os.path.isfile(proxy_path):
            try:
                os.remove(proxy_path)
            except Exception:
                pass
        ok = create_proxy(video_path, audio_path, proxy_path)
        if not ok:
            raise RuntimeError("Failed to create proxy video")

    if progress_callback:
        progress_callback("Analyzing canvas chunks…", 40)
    candidates = analyze_canvas(proxy_path, n_candidates=n_candidates, duration=2.5)

    total = len(candidates)
    for i, c in enumerate(candidates):
        clip_path = os.path.join(preview_dir, f"canvas_{i + 1}.mp4")
        if progress_callback:
            pct = 50 + int(45 * (i / max(total, 1)))
            progress_callback(f"Exporting canvas {i + 1}/{total}…", pct)
        if force or not os.path.isfile(clip_path):
            if os.path.isfile(clip_path):
                try:
                    os.remove(clip_path)
                except Exception:
                    pass
            export_preview_clip(proxy_path, c, clip_path, fade=0.0)
        c["preview_path"] = clip_path

    if progress_callback:
        progress_callback("Done", 100)

    return proxy_path, candidates

def extract_thumbnail(video_path: str, time_sec: float,
                      width: int = 320) -> Image.Image | None:
    """Extract a single frame as a PIL Image at the given timestamp."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w = frame.shape[:2]
    scale = width / w
    frame = cv2.resize(frame, (width, int(h * scale)))
    return Image.fromarray(frame)


# ---------------------------------------------------------------------------
# Embedded video player widget
# ---------------------------------------------------------------------------

class VideoPlayer:
    """
    Simple video player that displays frames on a tkinter Canvas.
    Audio is played via ffplay -nodisp in a subprocess.
    """

    def __init__(self, canvas, on_complete=None):
        self.canvas = canvas
        self.on_complete = on_complete
        self.cap: cv2.VideoCapture | None = None
        self.audio_proc: subprocess.Popen | None = None
        self.playing = False
        self.video_path = ""
        self.fps = 30.0
        self.frame_delay = 33  # ms
        self._photo = None  # prevent GC
        self._after_id = None
        self._loop = True

    def load(self, video_path: str):
        """Load a video file and show the first frame."""
        self.stop()
        self.video_path = video_path
        self.cap = cv2.VideoCapture(video_path)
        if not self.cap.isOpened():
            return
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_delay = max(10, int(1000 / self.fps))
        self._show_current_frame()

    def play(self):
        """Start playback with audio."""
        if not self.cap or self.playing:
            return
        self.playing = True
        # Reset to beginning
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        # Start audio via ffplay (no display window)
        try:
            self.audio_proc = subprocess.Popen(
                [get_binary_path("ffplay"), "-nodisp", "-autoexit", "-loglevel", "quiet",
                 self.video_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.audio_proc = None
        self._play_next()

    def pause(self):
        """Pause playback."""
        self.playing = False
        if self._after_id:
            self.canvas.after_cancel(self._after_id)
            self._after_id = None
        self._kill_audio()

    def stop(self):
        """Stop and reset."""
        self.playing = False
        if self._after_id:
            self.canvas.after_cancel(self._after_id)
            self._after_id = None
        self._kill_audio()
        if self.cap:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def destroy(self):
        """Clean up resources."""
        self.stop()
        if self.cap:
            self.cap.release()
            self.cap = None

    def _kill_audio(self):
        if self.audio_proc:
            try:
                self.audio_proc.kill()
                self.audio_proc.wait(timeout=1)
            except Exception:
                pass
            self.audio_proc = None

    def _play_next(self):
        if not self.playing or not self.cap:
            return
        ret = self._show_current_frame()
        if not ret:
            if self._loop:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._kill_audio()
                # Restart audio for loop
                try:
                    self.audio_proc = subprocess.Popen(
                        [get_binary_path("ffplay"), "-nodisp", "-autoexit", "-loglevel",
                         "quiet", self.video_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except FileNotFoundError:
                    pass
            else:
                self.stop()
                if self.on_complete:
                    self.on_complete()
                return
        self._after_id = self.canvas.after(self.frame_delay, self._play_next)

    def _show_current_frame(self) -> bool:
        if not self.cap:
            return False
        ret, frame = self.cap.read()
        if not ret:
            return False
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # Fit to canvas size
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 640, 360
        h, w = frame.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = int(w * scale), int(h * scale)
        if nw > 0 and nh > 0:
            frame = cv2.resize(frame, (nw, nh))
        img = Image.fromarray(frame)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(
            cw // 2, ch // 2, anchor="center", image=self._photo,
        )
        return True
