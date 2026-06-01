"""
Processing pipeline for the Music Video Pipeline Manager.

Executes the 6-step pipeline on a queued project in a background thread:
  1. Merge audio into video (ffmpeg — source has no audio track)
  2. Upscale via video_mod.py -r 4k
  3. (Store — output of step 2 is already in folder)
  4. Create loop via loop_finder.py with selected pick + fade
  5. Crop loop to 9:16 via video_mod.py --crop 9:16
  6. Add text overlay via text_overlay.py
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from pipeline import db
from pipeline import get_binary_path

# Root of the music_video project (where the scripts live)
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

def _run_python_script(script_name: str, args: list[str], log_callback=None) -> tuple[bool, str]:
    """Import and run a script's main() function in-process to work inside PyInstaller bundle."""
    import importlib
    import io
    from contextlib import redirect_stdout, redirect_stderr

    try:
        module = importlib.import_module(script_name)
    except Exception as e:
        return False, f"Failed to import {script_name}: {e}"

    orig_argv = sys.argv
    out = io.StringIO()
    err = io.StringIO()
    success = True
    stop_event = threading.Event()

    def log_poller():
        last_val = ""
        while not stop_event.is_set():
            time.sleep(0.5)
            val = (out.getvalue() + "\n" + err.getvalue()).strip()
            if val != last_val:
                last_val = val
                if log_callback:
                    try:
                        log_callback(val)
                    except Exception:
                        pass

    poller_thread = None
    if log_callback:
        poller_thread = threading.Thread(target=log_poller, daemon=True)
        poller_thread.start()

    try:
        sys.argv = [module.__file__] + args
        with redirect_stdout(out), redirect_stderr(err):
            module.main()
    except SystemExit as se:
        if se.code and se.code != 0:
            success = False
            err.write(f"\nExit code: {se.code}")
    except Exception as e:
        success = False
        import traceback
        traceback.print_exc(file=err)
    finally:
        sys.argv = orig_argv
        if poller_thread:
            stop_event.set()
            poller_thread.join(timeout=1.0)

    combined = out.getvalue() + "\n" + err.getvalue()
    return success, combined.strip()


STEP_NAMES = {
    1: "Merge FB Video",
    2: "Upscale FB Video",
    3: "Save FB Video",
    4: "Merge YT Video",
    5: "Upscale YT Video",
    6: "Save YT Video",
    7: "Create Loop",
    8: "Crop 9:16",
    9: "Text Overlay",
    10: "Upload Facebook Video",
    11: "Upload YouTube Video",
    12: "Upload YouTube Short",
}


def _python():
    """Return the Python interpreter path."""
    return sys.executable


def _run(cmd: list[str], label: str = "", log_callback=None) -> tuple[bool, str]:
    """Run a subprocess, streaming output to log_callback in real-time."""
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        
        output_lines = []
        for line in iter(process.stdout.readline, ""):
            output_lines.append(line)
            if log_callback:
                try:
                    log_callback("".join(output_lines).strip())
                except Exception:
                    pass
                    
        process.stdout.close()
        return_code = process.wait(timeout=7200)
        
        combined_output = "".join(output_lines).strip()
        if return_code != 0:
            return False, combined_output
        return True, combined_output
    except Exception as e:
        return False, f"{label}: {e}"


class PipelineWorker:
    """
    Background worker that processes queued projects one at a time.
    """

    def __init__(self, conn, on_update=None):
        self.conn = conn
        self.on_update = on_update  # callback(project_id, step, status, msg)
        self._thread: threading.Thread | None = None
        self._running = False
        # Initialize paused state from SQLite settings
        self._paused = (db.get_setting(self.conn, "queue_paused", "0") == "1")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def pause(self):
        self._paused = True
        db.set_setting(self.conn, "queue_paused", "1")

    def resume(self):
        self._paused = False
        db.set_setting(self.conn, "queue_paused", "0")

    def is_paused(self) -> bool:
        return self._paused

    def _notify(self, project_id: int, step: int,
                status: str, msg: str = ""):
        if self.on_update:
            try:
                self.on_update(project_id, step, status, msg)
            except Exception:
                pass

    def _worker_loop(self):
        """Continuously poll for queued projects and process them."""
        while self._running:
            if self._paused:
                time.sleep(1)
                continue
            queued = db.get_projects_by_status(self.conn, "queued")
            if not queued:
                time.sleep(2)
                continue
            project = queued[0]
            self._process_project(project)

    def _process_project(self, project: dict):
        pid = project["id"]
        folder = project["folder_path"]
        name = project["name"]

        db.update_project(self.conn, pid, status="processing", current_step=0, total_steps=9)
        self._notify(pid, 0, "started", f"Processing: {name}")

        try:
            # Initialize step logs
            for step_num, step_name in STEP_NAMES.items():
                db.log_step(self.conn, pid, step_num, step_name, "pending")

            # --- Step 1: Merge FB Video ---
            self._run_step(pid, 1, folder, project)

            # --- Step 2: Upscale FB Video ---
            self._run_step(pid, 2, folder, project)

            # --- Step 3: Save FB Video ---
            self._run_step(pid, 3, folder, project)

            # --- Step 4: Merge YT Video ---
            self._run_step(pid, 4, folder, project)

            # --- Step 5: Upscale YT Video ---
            self._run_step(pid, 5, folder, project)

            # --- Step 6: Save YT Video ---
            self._run_step(pid, 6, folder, project)

            # --- Step 7: Create Loop ---
            self._run_step(pid, 7, folder, project)

            # --- Step 8: Crop 9:16 ---
            self._run_step(pid, 8, folder, project)

            # --- Step 9: Text Overlay ---
            self._run_step(pid, 9, folder, project)

            db.update_project(self.conn, pid, status="pending_deployment", current_step=9)
            self._notify(pid, 9, "done", f"Complete: {name} (Pending Deployment)")

        except PipelineError as e:
            db.update_project(
                self.conn, pid,
                status="error",
                error_message=str(e),
            )
            self._notify(pid, e.step, "error", str(e))

    def _run_step(self, pid: int, step: int, folder: str, project: dict):
        step_name = STEP_NAMES[step]
        db.update_project(self.conn, pid, current_step=step)
        db.update_step(
            self.conn, pid, step,
            status="running",
            started_at=datetime.now().isoformat(),
        )
        self._notify(pid, step, "running", step_name)

        # Throttled callback to update database log in real-time and refresh UI at most once/sec
        last_update = [0.0]
        def log_cb(text: str):
            db.update_step(self.conn, pid, step, log_text=text)
            now = time.time()
            if now - last_update[0] >= 1.0:
                last_update[0] = now
                self._notify(pid, step, "running", step_name)

        try:
            output_file = ""
            log_text = ""

            if step == 1:
                output_file, log_text = self._step_merge_fb(folder, project, log_cb)
            elif step == 2:
                output_file, log_text = self._step_upscale_fb(folder, project, log_cb)
            elif step == 3:
                output_file, log_text = self._step_save_fb(folder, project)
            elif step == 4:
                output_file, log_text = self._step_merge_yt(folder, project, log_cb)
            elif step == 5:
                output_file, log_text = self._step_upscale_yt(folder, project, log_cb)
            elif step == 6:
                output_file, log_text = self._step_save_yt(folder, project)
            elif step == 7:
                output_file, log_text = self._step_loop(folder, project, log_cb)
            elif step == 8:
                output_file, log_text = self._step_crop(folder, project, log_cb)
            elif step == 9:
                output_file, log_text = self._step_text(folder, project, log_cb)

            db.update_step(
                self.conn, pid, step,
                status="done",
                finished_at=datetime.now().isoformat(),
                output_file=output_file,
                log_text=log_text,
            )
        except Exception as e:
            db.update_step(
                self.conn, pid, step,
                status="error",
                finished_at=datetime.now().isoformat(),
                log_text=str(e),
            )
            raise PipelineError(str(e), step)

    # ------------------------------------------------------------------
    # Individual steps
    # ------------------------------------------------------------------

    def _step_merge_fb(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 1: Merge Facebook Video (Short) with audio."""
        if not project.get("process_facebook", 1):
            return "Skipped", "Bypassed Facebook video merge (process_facebook disabled)"

        fb_video = project.get("facebook_video")
        audio = project["audio_file"]
        fb_audio = project.get("facebook_audio")
        name = project["name"]

        if not fb_video or not os.path.isfile(fb_video):
            raise RuntimeError(f"Facebook Video (Short) file not found: {fb_video}")
        fb_output = os.path.join(folder, f"{name} - Facebook.mp4")
        merge_audio = fb_audio if (fb_audio and os.path.isfile(fb_audio)) else audio
        if not merge_audio or not os.path.isfile(merge_audio):
            raise RuntimeError("Audio file for Facebook Video Merge not found")

        cmd_fb = [
            get_binary_path("ffmpeg"), "-y",
            "-i", fb_video,
            "-i", merge_audio,
            "-filter_complex", "[1:a]apad[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "320k",
            "-shortest",
            "-movflags", "+faststart",
            fb_output,
        ]
        ok_fb, log_fb = _run(cmd_fb, "Merge Facebook audio", log_callback)
        if not ok_fb:
            raise RuntimeError(f"Facebook Video Merge failed: {log_fb}")
        return fb_output, log_fb

    def _step_upscale_fb(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 2: Upscale Facebook Video (Short) to 4K."""
        if not project.get("process_facebook", 1):
            return "Skipped", "Bypassed Facebook upscale (process_facebook disabled)"

        name = project["name"]
        merged_fb = os.path.join(folder, f"{name} - Facebook.mp4")
        output_fb = os.path.join(folder, f"{name} 4k - Facebook.mp4")
        if not os.path.isfile(merged_fb):
            raise RuntimeError(f"Merged Facebook video not found: {merged_fb}")

        args = ["-i", merged_fb, "-o", output_fb, "-r", "4k", "--ai-upscale"]
        if project.get("negative_logo", 0):
            args.append("--negative-logo")
        ok, log = _run_python_script("video_mod", args, log_callback)
        if not ok:
            raise RuntimeError(f"Facebook Upscale failed: {log}")
        return output_fb, log

    def _step_save_fb(self, folder: str, project: dict) -> tuple[str, str]:
        """Step 3: Save Facebook Video."""
        if not project.get("process_facebook", 1):
            return "Skipped", "Bypassed Facebook save (process_facebook disabled)"

        name = project["name"]
        output_fb = os.path.join(folder, f"{name} 4k - Facebook.mp4")
        if os.path.isfile(output_fb):
            size_mb = os.path.getsize(output_fb) / (1024 * 1024)
            return output_fb, f"Saved Facebook 4K video: {output_fb} ({size_mb:.1f} MB)"
        return "", "Upscaled Facebook file not found"

    def _step_merge_yt(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 4: Merge YouTube Video (Long) with audio."""
        if not project.get("process_youtube", 1):
            return "Skipped", "Bypassed YouTube video merge (process_youtube disabled)"

        yt_video = project.get("youtube_video") or project["video_file"]
        audio = project["audio_file"]
        name = project["name"]

        yt_output = os.path.join(folder, f"{name} (Video).mp4")
        if not yt_video or not os.path.isfile(yt_video):
            raise RuntimeError(f"YouTube Video (Long) file not found: {yt_video}")
        if not audio or not os.path.isfile(audio):
            raise RuntimeError(f"Audio file not found: {audio}")

        cmd_yt = [
            get_binary_path("ffmpeg"), "-y",
            "-i", yt_video,
            "-i", audio,
            "-filter_complex", "[1:a]apad[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "320k",
            "-shortest",
            "-movflags", "+faststart",
            yt_output,
        ]
        ok_yt, log_yt = _run(cmd_yt, "Merge YouTube audio", log_callback)
        if not ok_yt:
            raise RuntimeError(f"YouTube Video Merge failed: {log_yt}")
        return yt_output, log_yt

    def _step_upscale_yt(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 5: Upscale YouTube Video (Long) to 4K."""
        if not project.get("process_youtube", 1):
            return "Skipped", "Bypassed YouTube upscale (process_youtube disabled)"

        name = project["name"]
        merged_yt = os.path.join(folder, f"{name} (Video).mp4")
        output_yt = os.path.join(folder, f"{name} 4k (Video).mp4")
        if not os.path.isfile(merged_yt):
            raise RuntimeError(f"Merged YouTube video not found: {merged_yt}")

        args = ["-i", merged_yt, "-o", output_yt, "-r", "4k", "--ai-upscale"]
        if project.get("negative_logo", 0):
            args.append("--negative-logo")
        ok, log = _run_python_script("video_mod", args, log_callback)
        if not ok:
            raise RuntimeError(f"YouTube Upscale failed: {log}")
        return output_yt, log

    def _step_save_yt(self, folder: str, project: dict) -> tuple[str, str]:
        """Step 6: Save YouTube Video."""
        if not project.get("process_youtube", 1):
            return "Skipped", "Bypassed YouTube save (process_youtube disabled)"

        name = project["name"]
        output_yt = os.path.join(folder, f"{name} 4k (Video).mp4")
        if os.path.isfile(output_yt):
            size_mb = os.path.getsize(output_yt) / (1024 * 1024)
            return output_yt, f"Saved YouTube 4K video: {output_yt} ({size_mb:.1f} MB)"
        return "", "Upscaled YouTube file not found"

    def _step_loop(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 7: Create Loop from the upscaled YouTube video."""
        if project.get("skip_shorts", 0):
            return "Skipped", "Bypassed loop creation (Shorts disabled)"

        name = project["name"]
        upscaled = os.path.join(folder, f"{name} 4k (Video).mp4")
        loop_file = os.path.join(folder, f"{name} 4k (Video) Loop.mp4")

        if not os.path.isfile(upscaled):
            raise RuntimeError(f"Upscaled video not found for loop: {upscaled}")

        pick = project.get("loop_pick", 1)
        fade = project.get("loop_fade", 2.0)

        ok, log = _run_python_script("loop_finder", [
            "-i", upscaled,
            "--export", loop_file,
            "--pick", str(pick),
            "--fade", str(fade),
        ], log_callback)
        if not ok:
            raise RuntimeError(f"Loop creation failed: {log}")
        return loop_file, log

    def _step_crop(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 8: Crop Loop Video to 9:16 aspect ratio."""
        if project.get("skip_shorts", 0):
            return "Skipped", "Bypassed crop (Shorts disabled)"

        name = project["name"]
        loop_file = os.path.join(folder, f"{name} 4k (Video) Loop.mp4")
        cropped = os.path.join(folder, f"{name} 4k (Video) Loop Crop.mp4")

        if not os.path.isfile(loop_file):
            raise RuntimeError(f"Loop video not found: {loop_file}")

        args = [
            "--crop", "9:16",
            "-i", loop_file,
            "-o", cropped,
        ]
        if project.get("negative_logo", 0):
            args.append("--negative-logo")
        ok, log = _run_python_script("video_mod", args, log_callback)
        if not ok:
            raise RuntimeError(f"Crop failed: {log}")
        return cropped, log

    def _step_text(self, folder: str, project: dict, log_callback=None) -> tuple[str, str]:
        """Step 9: Add Text Overlay to Cropped Video."""
        if project.get("skip_shorts", 0):
            return "Skipped", "Bypassed text overlay (Shorts disabled)"

        name = project["name"]
        cropped = os.path.join(folder, f"{name} 4k (Video) Loop Crop.mp4")
        output = os.path.join(folder, f"{name} - YouTube Short.mp4")

        if not os.path.isfile(cropped):
            raise RuntimeError(f"Cropped video not found: {cropped}")

        text_val = project.get("overlay_text", "")
        if not text_val:
            raise RuntimeError("No overlay text configured")

        font_size = project.get("font_size", 90)
        fi_start = project.get("fade_in_start", 2.0)
        fi_end = project.get("fade_in_end", 4.0)
        fo_start = project.get("fade_out_start", 8.0)
        fo_end = project.get("fade_out_end", 10.0)

        ok, log = _run_python_script("text_overlay", [
            "-i", cropped,
            "-o", output,
            "--text", text_val,
            "--fade-in", str(fi_start), str(fi_end),
            "--fade-out", str(fo_start), str(fo_end),
            "--fontsize", str(font_size),
        ], log_callback)
        if not ok:
            raise RuntimeError(f"Text overlay failed: {log}")
        return output, log


class PipelineError(Exception):
    def __init__(self, message: str, step: int = 0):
        super().__init__(message)
        self.step = step
