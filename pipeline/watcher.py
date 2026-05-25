"""
Folder watcher for the Music Video Pipeline Manager.

Uses the watchdog library to monitor a directory for new subfolders.
When a new folder appears it scans for video/audio files and creates
a staged project in the database.
"""

import os
import time
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from pipeline import db

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}


def scan_folder(folder_path: str) -> tuple[str, str, str, str]:
    """
    Scan a folder for video and audio files.
    Returns (facebook_video, youtube_video, facebook_audio, youtube_audio).
    """
    fb_video, yt_video, fb_audio, yt_audio = "", "", "", ""
    try:
        entries = sorted(os.listdir(folder_path))
    except OSError:
        return fb_video, yt_video, fb_audio, yt_audio

    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(folder_path, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext == ".mp4":
            if "short" in name.lower():
                if not fb_video:
                    fb_video = full
            else:
                if not yt_video:
                    yt_video = full
        elif ext == ".mp3":
            if "short" in name.lower():
                if not fb_audio:
                    fb_audio = full
            else:
                if not yt_audio:
                    yt_audio = full

    return fb_video, yt_video, fb_audio, yt_audio


class _NewFolderHandler(FileSystemEventHandler):
    """Detect new directories created inside the watch root."""

    def __init__(self, conn, on_new_project=None):
        super().__init__()
        self.conn = conn
        self.on_new_project = on_new_project
        # Debounce: ignore rapid duplicate events
        self._seen: dict[str, float] = {}

    def on_created(self, event):
        if not event.is_directory:
            return
        folder = event.src_path
        name = os.path.basename(folder)
        if name.startswith(".") or name.lower().endswith(".imovielibrary"):
            return
        if not os.path.isdir(folder):
            return
        now = time.time()
        if folder in self._seen and now - self._seen[folder] < 5:
            return
        self._seen[folder] = now

        # Only top-level subfolders (not nested)
        parent = os.path.dirname(folder)
        watch_root = db.get_setting(self.conn, "watch_folder", "").strip()
        if not watch_root or watch_root == "Not set" or not os.path.isdir(watch_root):
            return
        if os.path.normpath(parent) != os.path.normpath(watch_root):
            return

        self._register_folder(folder)

    def _register_folder(self, folder: str):
        # Skip if already registered
        if not os.path.isdir(folder):
            return
        if db.get_project_by_folder(self.conn, folder):
            return
        name = os.path.basename(folder)
        fb_video, yt_video, fb_audio, yt_audio = scan_folder(folder)
        pid = db.create_project(
            self.conn, folder, name,
            video_file=yt_video, audio_file=yt_audio,
            facebook_video=fb_video, youtube_video=yt_video,
            facebook_audio=fb_audio,
        )
        if pid and self.on_new_project:
            self.on_new_project(pid)


class FolderWatcher:
    """
    Watches a directory for new project subfolders.
    Runs the watchdog observer on a daemon thread.
    """

    def __init__(self, conn, on_new_project=None):
        self.conn = conn
        self.on_new_project = on_new_project
        self._observer: Observer | None = None
        self._handler = _NewFolderHandler(conn, on_new_project)

    def start(self, watch_folder: str):
        """Start watching. Idempotent — stops any previous watcher first."""
        self.stop()
        watch_folder = watch_folder.strip() if watch_folder else ""
        if not watch_folder or watch_folder == "Not set" or not os.path.isdir(watch_folder):
            return
        self._observer = Observer()
        self._observer.schedule(
            self._handler, watch_folder, recursive=False,
        )
        self._observer.daemon = True
        self._observer.start()

    def stop(self):
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=2)
        self._observer = None

    def scan_existing(self, watch_folder: str):
        """Register any existing subfolders that aren't tracked yet."""
        watch_folder = watch_folder.strip() if watch_folder else ""
        if not watch_folder or watch_folder == "Not set" or not os.path.isdir(watch_folder):
            return
        try:
            entries = sorted(os.listdir(watch_folder))
        except OSError:
            return
        for name in entries:
            if name.startswith(".") or name.lower().endswith(".imovielibrary"):
                continue
            full = os.path.join(watch_folder, name)
            if os.path.isdir(full):
                self._handler._register_folder(full)
