"""
Main GUI application for the Music Video Pipeline Manager.

Built with ttkbootstrap for a modern dark-themed desktop experience.
"""

import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.scrolled import ScrolledFrame
from ttkbootstrap import Style

from pipeline import db
from pipeline.watcher import FolderWatcher, scan_folder, VIDEO_EXTS, AUDIO_EXTS
from pipeline.pipeline import PipelineWorker, STEP_NAMES
from pipeline.preview import (
    generate_all_previews, VideoPlayer, extract_thumbnail,
)

from PIL import Image, ImageTk


DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pipeline_data", "pipeline.db",
)

STATUS_ICONS = {
    "staged": "📁",
    "queued": "🔶",
    "processing": "⏳",
    "done": "✅",
    "error": "❌",
}

STATUS_COLORS = {
    "staged": "info",
    "queued": "warning",
    "processing": "primary",
    "done": "success",
    "error": "danger",
}


class PipelineApp(ttk.Window):
    def __init__(self):
        super().__init__(
            title="Music Video Pipeline Manager",
            themename="darkly",
            size=(1400, 900),
            minsize=(1000, 700),
        )

        # Center on screen
        self.place_window_center()

        # Database
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.conn = db.init_db(DB_PATH)

        # Subsystems
        self.watcher = FolderWatcher(self.conn, on_new_project=self._on_new_project)
        self.worker = PipelineWorker(self.conn, on_update=self._on_pipeline_update)

        # State
        self.selected_project_id: int | None = None
        self._player: VideoPlayer | None = None
        self._loop_thumbnails: dict[int, ImageTk.PhotoImage] = {}

        self._build_ui()
        self._start_services()
        self._refresh_list()

        # Periodic refresh
        self.after(3000, self._periodic_refresh)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self, padding=10)
        top.pack(fill=X)

        ttk.Label(
            top, text="🎬 Music Video Pipeline",
            font=("Helvetica", 18, "bold"),
            bootstyle="inverse-dark",
        ).pack(side=LEFT)

        ttk.Button(
            top, text="⚙ Settings", bootstyle="secondary-outline",
            command=self._show_settings,
        ).pack(side=RIGHT, padx=5)

        self._pause_btn = ttk.Button(
            top, text="⏸ Pause Queue", bootstyle="warning-outline",
            command=self._toggle_queue_pause,
        )
        self._pause_btn.pack(side=RIGHT, padx=5)

        ttk.Button(
            top, text="+ Add Folder", bootstyle="success-outline",
            command=self._add_folder_manual,
        ).pack(side=RIGHT, padx=5)

        watch = db.get_setting(self.conn, "watch_folder", "Not set")
        self._watch_label = ttk.Label(
            top,
            text=f"📂 Watch: {watch}",
            font=("Helvetica", 10),
            bootstyle="secondary",
        )
        self._watch_label.pack(side=RIGHT, padx=15)

        # Main paned window
        paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        paned.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        # Left: project list
        left = ttk.Frame(paned, padding=5)
        paned.add(left, weight=1)

        ttk.Label(
            left, text="Projects",
            font=("Helvetica", 13, "bold"),
        ).pack(anchor=W, pady=(0, 5))

        # Search / filter
        filter_frame = ttk.Frame(left)
        filter_frame.pack(fill=X, pady=(0, 5))
        self._filter_var = tk.StringVar()
        ttk.Entry(
            filter_frame, textvariable=self._filter_var,
            bootstyle="dark",
        ).pack(fill=X)
        self._filter_var.trace_add("write", lambda *_: self._refresh_list())

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=BOTH, expand=True)

        self._tree = ttk.Treeview(
            list_frame, columns=("status",), show="tree",
            selectmode="browse", style="dark.Treeview",
        )
        self._tree.column("#0", width=250)
        self._tree.column("status", width=80, anchor=CENTER)
        self._tree.pack(fill=BOTH, expand=True, side=LEFT)

        tree_scroll = ttk.Scrollbar(
            list_frame, orient=VERTICAL, command=self._tree.yview,
        )
        tree_scroll.pack(fill=Y, side=RIGHT)
        self._tree.configure(yscrollcommand=tree_scroll.set)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # Right: detail panel
        right = ttk.Frame(paned, padding=10)
        paned.add(right, weight=3)

        self._detail_frame = right
        self._detail_content: ttk.Frame | None = None

        self._show_empty_detail()

    # ------------------------------------------------------------------
    # Detail Views
    # ------------------------------------------------------------------

    def _clear_detail(self):
        if hasattr(self, "_log_text_widget") and self._log_text_widget and self._log_text_widget.winfo_exists():
            try:
                self._last_log_yview = self._log_text_widget.yview()
                self._last_log_project_id = getattr(self, "_current_detail_project_id", None)
            except Exception:
                self._last_log_yview = None
                self._last_log_project_id = None
        else:
            self._last_log_yview = None
            self._last_log_project_id = None

        if self._player:
            self._player.destroy()
            self._player = None
        for child in self._detail_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self._detail_content = None

    def _show_empty_detail(self):
        self._clear_detail()
        f = ttk.Frame(self._detail_frame)
        f.pack(fill=BOTH, expand=True)
        self._detail_content = f
        ttk.Label(
            f, text="Select a project from the list",
            font=("Helvetica", 14),
            bootstyle="secondary",
        ).pack(expand=True)

    def _show_project_detail(self, project: dict):
        self._clear_detail()

        scroll = ScrolledFrame(self._detail_frame, autohide=True)
        scroll.pack(fill=BOTH, expand=True)
        self._detail_content = scroll

        f = ttk.Frame(scroll, padding=(0, 0, 20, 0))
        f.pack(fill=BOTH, expand=True)

        status = project["status"]
        name = project["name"]
        pid = project["id"]

        # Header
        header = ttk.Frame(f)
        header.pack(fill=X, pady=(0, 15))

        ttk.Label(
            header,
            text=f"{STATUS_ICONS.get(status, '')}  {name}",
            font=("Helvetica", 16, "bold"),
        ).pack(side=LEFT)

        badge_style = STATUS_COLORS.get(status, "info")
        ttk.Label(
            header,
            text=f" {status.upper()} ",
            font=("Helvetica", 10, "bold"),
            bootstyle=f"inverse-{badge_style}",
        ).pack(side=RIGHT)

        # Error message
        if status == "error" and project.get("error_message"):
            err_frame = ttk.Frame(f)
            err_frame.pack(fill=X, pady=(0, 10))
            ttk.Label(
                err_frame,
                text=f"⚠ {project['error_message']}",
                bootstyle="danger",
                wraplength=700,
            ).pack(fill=X)
            ttk.Button(
                err_frame, text="Retry",
                bootstyle="warning",
                command=lambda: self._queue_project(pid),
            ).pack(anchor=E, pady=5)

        # Processing progress
        if status == "processing":
            self._show_processing_progress(f, project)
            return

        # Done summary
        if status == "done":
            self._show_done_summary(f, project)
            return

        # ---- Editable fields (staged / queued / error) ----
        is_editable = status in ("staged", "error", "queued")

        # Folder path
        ttk.Label(
            f, text=f"📁 {project['folder_path']}",
            font=("Helvetica", 9), bootstyle="secondary",
        ).pack(anchor=W, pady=(0, 10))

        # Separator
        ttk.Separator(f).pack(fill=X, pady=5)

        # Facebook Video (Short)
        fb_vid_frame = ttk.Frame(f)
        fb_vid_frame.pack(fill=X, pady=5)
        ttk.Label(fb_vid_frame, text="FB Video (Short):", width=15).pack(side=LEFT)
        self._fb_video_var = tk.StringVar(value=project.get("facebook_video", ""))
        fb_vid_entry = ttk.Entry(
            fb_vid_frame, textvariable=self._fb_video_var,
            state="normal" if is_editable else "readonly",
        )
        fb_vid_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        if is_editable:
            ttk.Button(
                fb_vid_frame, text="Browse", bootstyle="secondary-outline",
                command=lambda: self._browse_file(
                    self._fb_video_var, "Facebook Video (Short)", VIDEO_EXTS,
                    initialdir=project['folder_path']
                ),
            ).pack(side=RIGHT)

        # Facebook Audio (Short)
        fb_aud_frame = ttk.Frame(f)
        fb_aud_frame.pack(fill=X, pady=5)
        ttk.Label(fb_aud_frame, text="FB Audio (Short):", width=15).pack(side=LEFT)
        self._fb_audio_var = tk.StringVar(value=project.get("facebook_audio", ""))
        fb_aud_entry = ttk.Entry(
            fb_aud_frame, textvariable=self._fb_audio_var,
            state="normal" if is_editable else "readonly",
        )
        fb_aud_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        if is_editable:
            ttk.Button(
                fb_aud_frame, text="Browse", bootstyle="secondary-outline",
                command=lambda: self._browse_file(
                    self._fb_audio_var, "Facebook Audio (Short)", AUDIO_EXTS,
                    initialdir=project['folder_path']
                ),
            ).pack(side=RIGHT)

        # YouTube Video (Long)
        yt_vid_frame = ttk.Frame(f)
        yt_vid_frame.pack(fill=X, pady=5)
        ttk.Label(yt_vid_frame, text="YT Video (Long):", width=15).pack(side=LEFT)
        self._yt_video_var = tk.StringVar(value=project.get("youtube_video", "") or project.get("video_file", ""))
        yt_vid_entry = ttk.Entry(
            yt_vid_frame, textvariable=self._yt_video_var,
            state="normal" if is_editable else "readonly",
        )
        yt_vid_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        if is_editable:
            ttk.Button(
                yt_vid_frame, text="Browse", bootstyle="secondary-outline",
                command=lambda: self._browse_file(
                    self._yt_video_var, "YouTube Video (Long)", VIDEO_EXTS,
                    initialdir=project['folder_path']
                ),
            ).pack(side=RIGHT)

        # YouTube Audio (Long)
        aud_frame = ttk.Frame(f)
        aud_frame.pack(fill=X, pady=5)
        ttk.Label(aud_frame, text="YT Audio (Long):", width=15).pack(side=LEFT)
        self._audio_var = tk.StringVar(value=project.get("audio_file", ""))
        aud_entry = ttk.Entry(
            aud_frame, textvariable=self._audio_var,
            state="normal" if is_editable else "readonly",
        )
        aud_entry.pack(side=LEFT, fill=X, expand=True, padx=5)
        if is_editable:
            ttk.Button(
                aud_frame, text="Browse", bootstyle="secondary-outline",
                command=lambda: self._browse_file(
                    self._audio_var, "YouTube Audio (Long)", AUDIO_EXTS,
                    initialdir=project['folder_path']
                ),
            ).pack(side=RIGHT)

        # Process Options
        settings_frame = ttk.Frame(f)
        settings_frame.pack(fill=X, pady=5)

        self._proc_fb_var = tk.BooleanVar(value=bool(project.get("process_facebook", 1)))
        self._proc_fb_chk = ttk.Checkbutton(
            settings_frame, text="Process FB Video",
            variable=self._proc_fb_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_fb_chk.pack(side=LEFT, padx=(0, 20))

        self._proc_yt_var = tk.BooleanVar(value=bool(project.get("process_youtube", 1)))
        self._proc_yt_chk = ttk.Checkbutton(
            settings_frame, text="Process YT Video",
            variable=self._proc_yt_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_yt_chk.pack(side=LEFT, padx=(0, 20))

        self._proc_shorts_var = tk.BooleanVar(value=not bool(project.get("skip_shorts", 0)))
        self._proc_shorts_chk = ttk.Checkbutton(
            settings_frame, text="Process YT Short (Steps 4-6)",
            variable=self._proc_shorts_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_shorts_chk.pack(side=LEFT)

        # Separator
        ttk.Separator(f).pack(fill=X, pady=10)

        # Text overlay
        ttk.Label(
            f, text="YouTube Short Text:",
            font=("Helvetica", 11, "bold"),
        ).pack(anchor=W, pady=(0, 5))

        self._text_widget = tk.Text(
            f, height=3, wrap="word",
            bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0",
            font=("Helvetica", 11),
            state="normal" if is_editable else "disabled",
        )
        self._text_widget.pack(fill=X, pady=(0, 5))
        self._text_widget.insert("1.0", project.get("overlay_text", ""))

        # Text params row
        params = ttk.Frame(f)
        params.pack(fill=X, pady=5)

        ttk.Label(params, text="Font Size:").pack(side=LEFT)
        self._fontsize_var = tk.IntVar(value=project.get("font_size", 90))
        ttk.Spinbox(
            params, from_=20, to=200,
            textvariable=self._fontsize_var, width=6,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 20))

        ttk.Label(params, text="Fade In:").pack(side=LEFT)
        self._fi_start_var = tk.DoubleVar(
            value=project.get("fade_in_start", 2.0))
        ttk.Spinbox(
            params, from_=0, to=30, increment=0.5,
            textvariable=self._fi_start_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)
        ttk.Label(params, text="→").pack(side=LEFT)
        self._fi_end_var = tk.DoubleVar(
            value=project.get("fade_in_end", 4.0))
        ttk.Spinbox(
            params, from_=0, to=30, increment=0.5,
            textvariable=self._fi_end_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(2, 20))

        ttk.Label(params, text="Fade Out:").pack(side=LEFT)
        self._fo_start_var = tk.DoubleVar(
            value=project.get("fade_out_start", 8.0))
        ttk.Spinbox(
            params, from_=0, to=60, increment=0.5,
            textvariable=self._fo_start_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)
        ttk.Label(params, text="→").pack(side=LEFT)
        self._fo_end_var = tk.DoubleVar(
            value=project.get("fade_out_end", 10.0))
        ttk.Spinbox(
            params, from_=0, to=60, increment=0.5,
            textvariable=self._fo_end_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)

        # Separator
        ttk.Separator(f).pack(fill=X, pady=10)

        # Loop section
        loop_header = ttk.Frame(f)
        loop_header.pack(fill=X, pady=(0, 5))

        ttk.Label(
            loop_header, text="Loop Selection",
            font=("Helvetica", 11, "bold"),
        ).pack(side=LEFT)

        if is_editable:
            self._analyze_btn = ttk.Button(
                loop_header, text="▶ Analyze Loops",
                bootstyle="info",
                command=lambda: self._analyze_loops(pid),
            )
            self._analyze_btn.pack(side=RIGHT)

        # Loop params
        loop_params = ttk.Frame(f)
        loop_params.pack(fill=X, pady=5)

        ttk.Label(loop_params, text="Fade (sec):").pack(side=LEFT)
        self._loop_fade_var = tk.DoubleVar(
            value=project.get("loop_fade", 2.0))
        ttk.Spinbox(
            loop_params, from_=0, to=10, increment=0.5,
            textvariable=self._loop_fade_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 20))

        ttk.Label(loop_params, text="Selected Loop:").pack(side=LEFT)
        self._loop_pick_var = tk.IntVar(
            value=project.get("loop_pick", 1))
        self._loop_pick_spin = ttk.Spinbox(
            loop_params, from_=1, to=10,
            textvariable=self._loop_pick_var, width=4,
            state="normal" if is_editable else "readonly",
        )
        self._loop_pick_spin.pack(side=LEFT, padx=5)

        # Video preview player
        player_frame = ttk.LabelFrame(
            f, text="Loop Preview",
        )
        player_frame.pack(fill=X, pady=10)

        self._player_canvas = tk.Canvas(
            player_frame, bg="#0d0d1a", highlightthickness=0,
            height=300,
        )
        self._player_canvas.pack(fill=X)

        player_controls = ttk.Frame(player_frame)
        player_controls.pack(fill=X, pady=5)

        self._play_btn = ttk.Button(
            player_controls, text="▶ Play", bootstyle="success",
            command=self._toggle_play,
        )
        self._play_btn.pack(side=LEFT, padx=5)

        self._stop_btn = ttk.Button(
            player_controls, text="⏹ Stop", bootstyle="danger-outline",
            command=self._stop_player,
        )
        self._stop_btn.pack(side=LEFT)

        self._player_status = ttk.Label(
            player_controls, text="", bootstyle="secondary",
        )
        self._player_status.pack(side=LEFT, padx=10)

        self._player = VideoPlayer(self._player_canvas)

        # Loop candidates list
        self._candidates_frame = ttk.Frame(f)
        self._candidates_frame.pack(fill=X, pady=5)

        # Load existing candidates
        candidates_json = project.get("loop_candidates_json", "[]")
        try:
            candidates = json.loads(candidates_json)
        except (json.JSONDecodeError, TypeError):
            candidates = []

        if candidates:
            self._show_candidates(candidates, is_editable)

        # Separator
        ttk.Separator(f).pack(fill=X, pady=10)

        # Action buttons
        if is_editable:
            actions = ttk.Frame(f)
            actions.pack(fill=X, pady=10)

            ttk.Button(
                actions, text="💾 Save Settings",
                bootstyle="info",
                command=lambda: self._save_project(pid),
            ).pack(side=LEFT, padx=5)

            if status == "queued":
                ttk.Button(
                    actions, text="📥 Dequeue",
                    bootstyle="warning-outline",
                    command=lambda: self._dequeue_project(pid),
                ).pack(side=LEFT, padx=5)
            else:
                ttk.Button(
                    actions, text="✅ Queue for Processing",
                    bootstyle="success",
                    command=lambda: self._save_and_queue(pid),
                ).pack(side=LEFT, padx=5)

            ttk.Button(
                actions, text="🏁 Mark Done",
                bootstyle="success-outline",
                command=lambda: self._mark_project_done_direct(pid),
            ).pack(side=LEFT, padx=5)

            ttk.Button(
                actions, text="🗑 Delete Project",
                bootstyle="danger-outline",
                command=lambda: self._delete_project(pid),
            ).pack(side=RIGHT, padx=5)

        self._add_log_viewer(f, pid)

    def _show_candidates(self, candidates: list[dict],
                         is_editable: bool = True):
        """Display loop candidates with play buttons."""
        for w in self._candidates_frame.winfo_children():
            w.destroy()

        ttk.Label(
            self._candidates_frame,
            text=f"{len(candidates)} loop candidates found:",
            font=("Helvetica", 10),
            bootstyle="secondary",
        ).pack(anchor=W, pady=(0, 5))

        for i, c in enumerate(candidates):
            row = ttk.Frame(self._candidates_frame)
            row.pack(fill=X, pady=2)

            num = i + 1
            start_m, start_s = divmod(c["start_time"], 60)
            end_m, end_s = divmod(c["end_time"], 60)
            dur_m, dur_s = divmod(c["duration"], 60)

            text = (
                f"#{num}  "
                f"{int(start_m)}:{start_s:05.2f} → "
                f"{int(end_m)}:{end_s:05.2f}  "
                f"({int(dur_m)}:{dur_s:05.2f})  "
                f"score: {c['score']:.3f}"
            )

            # Select radio
            if is_editable:
                ttk.Radiobutton(
                    row, text=text,
                    variable=self._loop_pick_var, value=num,
                    bootstyle="info",
                ).pack(side=LEFT)
            else:
                ttk.Label(row, text=text).pack(side=LEFT)

            # Play button
            preview_path = c.get("preview_path", "")
            if preview_path and os.path.isfile(preview_path):
                ttk.Button(
                    row, text="▶",
                    bootstyle="success-outline",
                    width=3,
                    command=lambda p=preview_path: self._play_preview(p),
                ).pack(side=RIGHT, padx=5)

    def _show_processing_progress(self, parent, project: dict):
        """Show step-by-step progress for a processing project."""
        current = project.get("current_step", 0)
        total = project.get("total_steps", 9)

        # Progress bar
        pct = int(100 * current / max(total, 1))
        ttk.Label(
            parent,
            text=f"Progress: Step {current}/{total}",
            font=("Helvetica", 12),
        ).pack(anchor=W, pady=5)

        bar = ttk.Progressbar(
            parent, maximum=100, value=pct,
            bootstyle="success-striped",
            length=600,
        )
        bar.pack(fill=X, pady=5)

        # Step list
        steps = db.get_steps(self.conn, project["id"])
        for step in steps:
            row = ttk.Frame(parent)
            row.pack(fill=X, pady=2)

            icon = "⬜"
            style = "secondary"
            if step["status"] == "done":
                icon = "✅"
                style = "success"
            elif step["status"] == "running":
                icon = "⏳"
                style = "info"
            elif step["status"] == "error":
                icon = "❌"
                style = "danger"

            ttk.Label(
                row,
                text=f"{icon}  Step {step['step_num']}: {step['step_name']}",
                bootstyle=style,
            ).pack(side=LEFT)

            if step.get("output_file"):
                ttk.Label(
                    row,
                    text=os.path.basename(step["output_file"]),
                    bootstyle="secondary",
                    font=("Helvetica", 9),
                ).pack(side=RIGHT)

        self._add_log_viewer(parent, project["id"])

    def _show_done_summary(self, parent, project: dict):
        """Show completion summary with output files."""
        ttk.Label(
            parent,
            text="✅ Processing Complete!",
            font=("Helvetica", 14, "bold"),
            bootstyle="success",
        ).pack(anchor=W, pady=(0, 10))

        steps = db.get_steps(self.conn, project["id"])
        for step in steps:
            row = ttk.Frame(parent)
            row.pack(fill=X, pady=3)

            ttk.Label(
                row,
                text=f"✅ {step['step_name']}",
                bootstyle="success",
            ).pack(side=LEFT)

            out = step.get("output_file", "")
            if out:
                files = [f.strip() for f in out.split(",") if f.strip()]
                for f in files:
                    if os.path.isfile(f):
                        size = os.path.getsize(f) / (1024 * 1024)
                        ttk.Label(
                            row,
                            text=f"{os.path.basename(f)} ({size:.1f} MB)",
                            bootstyle="secondary",
                            font=("Helvetica", 9),
                        ).pack(side=RIGHT, padx=5)

        ttk.Separator(parent).pack(fill=X, pady=10)

        ttk.Button(
            parent, text="📂 Open Folder",
            bootstyle="info-outline",
            command=lambda: self._open_folder(project["folder_path"]),
        ).pack(anchor=W)

        # Allow re-queue
        ttk.Button(
            parent, text="🔄 Re-process",
            bootstyle="warning-outline",
            command=lambda: self._queue_project(project["id"]),
        ).pack(anchor=W, pady=5)

        self._add_log_viewer(parent, project["id"])

    def _add_log_viewer(self, parent, project_id: int):
        """Add a scrollable log viewer for completed/running steps."""
        steps = db.get_steps(self.conn, project_id)
        active_steps = [s for s in steps if s["status"] in ("done", "running", "error")]
        if not active_steps:
            return

        lf = ttk.LabelFrame(parent, text="Pipeline Execution Logs")
        lf.pack(fill=BOTH, expand=True, pady=10)

        text = tk.Text(
            lf, height=12, wrap="word",
            bg="#1e1e2e", fg="#abe9b3", insertbackground="#e0e0e0",
            font=("Courier", 10),
        )
        text.pack(fill=BOTH, expand=True, side=LEFT, padx=5, pady=5)

        scroll = ttk.Scrollbar(lf, orient=VERTICAL, command=text.yview)
        scroll.pack(fill=Y, side=RIGHT, pady=5, padx=(0, 5))
        text.configure(yscrollcommand=scroll.set)

        self._log_text_widget = text
        self._current_detail_project_id = project_id

        log_content = []
        for step in steps:
            if step["status"] in ("done", "running", "error"):
                status_str = step["status"].upper()
                log_content.append(f"=== Step {step['step_num']}: {step['step_name']} ({status_str}) ===")
                if step.get("log_text"):
                    log_content.append(step["log_text"])
                else:
                    log_content.append("[No log output yet]")
                log_content.append("\n" + "="*60 + "\n")

        full_text = "\n".join(log_content)
        text.insert("1.0", full_text)
        text.configure(state="disabled")

        if getattr(self, "_last_log_project_id", None) == project_id and getattr(self, "_last_log_yview", None) is not None:
            if self._last_log_yview[1] >= 0.95:
                text.see("end")
            else:
                text.yview_moveto(self._last_log_yview[0])
        else:
            text.see("end")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _browse_file(self, var: tk.StringVar, label: str,
                     extensions: set[str], initialdir: str | None = None):
        exts = " ".join(f"*{e}" for e in sorted(extensions))
        path = filedialog.askopenfilename(
            title=f"Select {label} File",
            filetypes=[(f"{label} files", exts), ("All files", "*.*")],
            initialdir=initialdir,
        )
        if path:
            var.set(path)

    def _save_project(self, pid: int):
        """Save current form values to the database."""
        text = self._text_widget.get("1.0", "end-1c").strip()
        db.update_project(
            self.conn, pid,
            facebook_video=self._fb_video_var.get(),
            youtube_video=self._yt_video_var.get(),
            video_file=self._yt_video_var.get(),
            audio_file=self._audio_var.get(),
            facebook_audio=self._fb_audio_var.get(),
            skip_shorts=int(not self._proc_shorts_var.get()),
            process_facebook=int(self._proc_fb_var.get()),
            process_youtube=int(self._proc_yt_var.get()),
            overlay_text=text,
            font_size=self._fontsize_var.get(),
            fade_in_start=self._fi_start_var.get(),
            fade_in_end=self._fi_end_var.get(),
            fade_out_start=self._fo_start_var.get(),
            fade_out_end=self._fo_end_var.get(),
            loop_pick=self._loop_pick_var.get(),
            loop_fade=self._loop_fade_var.get(),
        )
        self._refresh_list()

    def _save_and_queue(self, pid: int):
        """Validate, save, and move to queue."""
        # Validate
        fb_video = self._fb_video_var.get()
        yt_video = self._yt_video_var.get()
        audio = self._audio_var.get()
        fb_audio = self._fb_audio_var.get()
        text = self._text_widget.get("1.0", "end-1c").strip()
        proc_shorts = self._proc_shorts_var.get()
        proc_fb = self._proc_fb_var.get()
        proc_yt = self._proc_yt_var.get()

        errors = []
        if not proc_fb and not proc_yt and not proc_shorts:
            errors.append("At least one target (Facebook, YouTube, or YouTube Short) must be enabled for processing")

        if proc_yt or proc_shorts:
            if not yt_video or not os.path.isfile(yt_video):
                errors.append("YouTube Video (Long) file is missing or not found")
            if not audio or not os.path.isfile(audio):
                errors.append("YouTube Audio (Long) file is missing or not found")
            if proc_shorts and not text:
                errors.append("YouTube short text is empty")

        if proc_fb:
            if not fb_video or not os.path.isfile(fb_video):
                errors.append("Facebook Video (Short) file is missing or not found")
            if fb_audio:
                if not os.path.isfile(fb_audio):
                    errors.append("Facebook Audio (Short) file not found")
            else:
                if not audio or not os.path.isfile(audio):
                    errors.append("Facebook Audio is missing, and YouTube Audio (fallback) is missing or not found")

        if errors:
            messagebox.showwarning(
                "Cannot Queue",
                "Please fix the following:\n\n• " + "\n• ".join(errors),
            )
            return

        self._save_project(pid)
        self._queue_project(pid)

    def _queue_project(self, pid: int):
        db.update_project(
            self.conn, pid,
            status="queued",
            current_step=0,
            queued_at=datetime.now().isoformat(),
            error_message="",
        )
        # Clear old process logs
        self.conn.execute(
            "DELETE FROM process_log WHERE project_id = ?", (pid,),
        )
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _dequeue_project(self, pid: int):
        db.update_project(
            self.conn, pid,
            status="staged",
            current_step=0,
            queued_at=None,
            error_message="",
        )
        # Clear old process logs
        self.conn.execute(
            "DELETE FROM process_log WHERE project_id = ?", (pid,),
        )
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _mark_project_done_direct(self, pid: int):
        if not messagebox.askyesno(
            "Mark Done",
            "Mark this project completed directly?\n"
            "This will bypass all background processing steps.",
        ):
            return
        db.update_project(
            self.conn, pid,
            status="done",
            current_step=9,
            error_message="",
        )
        # Seed fake/noop steps in the process log so it displays correctly
        self.conn.execute("DELETE FROM process_log WHERE project_id = ?", (pid,))
        for step_num in range(1, 10):
            self.conn.execute(
                """INSERT INTO process_log 
                   (project_id, step_num, step_name, status, output_file, log_text)
                   VALUES (?, ?, ?, 'done', 'Bypassed', 'Manually marked as completed')""",
                (pid, step_num, STEP_NAMES[step_num]),
            )
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _delete_project(self, pid: int):
        if not messagebox.askyesno(
            "Delete Project",
            "Remove this project from the pipeline?\n"
            "(The folder and files will NOT be deleted.)",
        ):
            return
        self._stop_player()
        db.delete_project(self.conn, pid)
        self.selected_project_id = None
        self._refresh_list()
        self._show_empty_detail()

    def _add_folder_manual(self):
        folder = filedialog.askdirectory(title="Select Project Folder")
        if not folder:
            return
        existing = db.get_project_by_folder(self.conn, folder)
        if existing:
            messagebox.showinfo("Already Tracked", "This folder is already a project.")
            self._select_project(existing["id"])
            return
        name = os.path.basename(folder)
        fb_video, yt_video, fb_audio, yt_audio = scan_folder(folder)
        pid = db.create_project(
            self.conn, folder, name,
            video_file=yt_video, audio_file=yt_audio,
            facebook_video=fb_video, youtube_video=yt_video,
            facebook_audio=fb_audio,
        )
        self._refresh_list()
        if pid:
            self._select_project(pid)

    def _open_folder(self, path: str):
        if sys.platform == "darwin":
            os.system(f'open "{path}"')
        elif sys.platform == "win32":
            os.startfile(path)
        else:
            os.system(f'xdg-open "{path}"')

    # ------------------------------------------------------------------
    # Loop Analysis
    # ------------------------------------------------------------------

    def _analyze_loops(self, pid: int):
        video = self._yt_video_var.get()
        audio = self._audio_var.get()

        if not video or not os.path.isfile(video):
            messagebox.showwarning("Missing", "Please select a YouTube Video (Long) first.")
            return
        if not audio or not os.path.isfile(audio):
            messagebox.showwarning("Missing", "Please select an audio file first.")
            return

        project = db.get_project(self.conn, pid)
        if not project:
            return

        # Save current values first
        self._save_project(pid)

        # Disable button during analysis
        if hasattr(self, "_analyze_btn"):
            self._analyze_btn.configure(
                state="disabled", text="Analyzing…",
            )

        def _work():
            try:
                proxy_path, candidates = generate_all_previews(
                    video, audio, project["folder_path"],
                    n_candidates=5,
                    progress_callback=lambda msg, pct: self.after(
                        0, lambda m=msg: self._update_analyze_status(m),
                    ),
                )
                # Save to DB
                candidates_json = json.dumps(candidates, default=str)
                db.update_project(
                    self.conn, pid,
                    proxy_path=proxy_path,
                    loop_candidates_json=candidates_json,
                )
                # Refresh UI on main thread
                self.after(0, lambda: self._on_analysis_complete(pid))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Analysis Failed", str(e),
                ))
                self.after(0, lambda: self._reset_analyze_btn())

        threading.Thread(target=_work, daemon=True).start()

    def _update_analyze_status(self, msg: str):
        if hasattr(self, "_analyze_btn"):
            self._analyze_btn.configure(text=msg)

    def _reset_analyze_btn(self):
        if hasattr(self, "_analyze_btn"):
            self._analyze_btn.configure(
                state="normal", text="▶ Analyze Loops",
            )

    def _on_analysis_complete(self, pid: int):
        self._reset_analyze_btn()
        self._select_project(pid)

    # ------------------------------------------------------------------
    # Video Player
    # ------------------------------------------------------------------

    def _play_preview(self, path: str):
        """Load and play a preview clip in the embedded player."""
        if self._player:
            self._player.load(path)
            self._player.play()
            self._play_btn.configure(text="⏸ Pause")
            self._player_status.configure(
                text=f"Playing: {os.path.basename(path)}",
            )

    def _toggle_play(self):
        if self._player:
            if self._player.playing:
                self._player.pause()
                self._play_btn.configure(text="▶ Play")
            else:
                self._player.play()
                self._play_btn.configure(text="⏸ Pause")

    def _stop_player(self):
        if self._player:
            self._player.stop()
            self._play_btn.configure(text="▶ Play")
            self._player_status.configure(text="")

    # ------------------------------------------------------------------
    # Project List
    # ------------------------------------------------------------------

    def _refresh_list(self):
        """Rebuild the treeview from the database while preserving expanded group states."""
        # Query current group open states before clearing
        open_states = {}
        for status in ["processing", "queued", "staged", "error", "done"]:
            iid = f"group_{status}"
            if self._tree.exists(iid):
                open_states[status] = bool(self._tree.item(iid, "open"))
            else:
                # Default states
                open_states[status] = status in ("staged", "queued", "processing", "error")

        self._tree.delete(*self._tree.get_children())
        projects = db.get_all_projects(self.conn)

        filter_text = self._filter_var.get().lower()

        # Group by status
        groups = {
            "processing": [],
            "queued": [],
            "staged": [],
            "error": [],
            "done": [],
        }
        for p in projects:
            s = p["status"]
            if s not in groups:
                groups[s] = []
            groups[s].append(p)

        for status, items in groups.items():
            if not items:
                continue
            label = f"  {STATUS_ICONS.get(status, '')}  {status.upper()}  ({len(items)})"
            group_id = self._tree.insert(
                "", "end", iid=f"group_{status}", text=label,
                tags=(f"group_{status}",),
                open=open_states.get(status, status in ("staged", "queued", "processing", "error")),
            )

            for p in items:
                if filter_text and filter_text not in p["name"].lower():
                    continue
                display = p["name"]
                if status == "processing":
                    step = p.get("current_step", 0)
                    display += f"  (Step {step}/6)"
                self._tree.insert(
                    group_id, "end",
                    text=f"  {display}",
                    values=(p["id"],),
                    tags=(f"item_{status}",),
                )

        # Style tags
        self._tree.tag_configure("group_staged", foreground="#5bc0de")
        self._tree.tag_configure("group_queued", foreground="#f0ad4e")
        self._tree.tag_configure("group_processing", foreground="#5bc0de")
        self._tree.tag_configure("group_done", foreground="#5cb85c")
        self._tree.tag_configure("group_error", foreground="#d9534f")
        self._tree.tag_configure("item_staged", foreground="#adb5bd")
        self._tree.tag_configure("item_queued", foreground="#f0ad4e")
        self._tree.tag_configure("item_processing", foreground="#5bc0de")
        self._tree.tag_configure("item_done", foreground="#5cb85c")
        self._tree.tag_configure("item_error", foreground="#d9534f")

    def _on_select(self, event):
        sel = self._tree.selection()
        if not sel:
            return
        values = self._tree.item(sel[0], "values")
        if not values:
            return
        try:
            pid = int(values[0])
        except (ValueError, IndexError):
            return
        self._select_project(pid)

    def _select_project(self, pid: int):
        self.selected_project_id = pid
        project = db.get_project(self.conn, pid)
        if project:
            self._show_project_detail(project)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _show_settings(self):
        dlg = ttk.Toplevel(self)
        dlg.title("Settings")
        dlg.geometry("500x250")
        dlg.transient(self)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=20)
        f.pack(fill=BOTH, expand=True)

        ttk.Label(
            f, text="Settings",
            font=("Helvetica", 14, "bold"),
        ).pack(anchor=W, pady=(0, 15))

        # Watch folder
        wf_frame = ttk.Frame(f)
        wf_frame.pack(fill=X, pady=5)
        ttk.Label(wf_frame, text="Watch Folder:").pack(side=LEFT)
        watch_var = tk.StringVar(
            value=db.get_setting(self.conn, "watch_folder", ""),
        )
        ttk.Entry(wf_frame, textvariable=watch_var).pack(
            side=LEFT, fill=X, expand=True, padx=5,
        )
        ttk.Button(
            wf_frame, text="Browse",
            bootstyle="secondary-outline",
            command=lambda: watch_var.set(
                filedialog.askdirectory(title="Select Watch Folder") or
                watch_var.get(),
            ),
        ).pack(side=RIGHT)

        def save():
            wf = watch_var.get()
            db.set_setting(self.conn, "watch_folder", wf)
            self._watch_label.configure(text=f"📂 Watch: {wf or 'Not set'}")
            self._restart_watcher()
            dlg.destroy()

        ttk.Button(
            f, text="Save", bootstyle="success",
            command=save,
        ).pack(anchor=E, pady=15)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    def _start_services(self):
        """Start folder watcher and pipeline worker."""
        # Reset stuck processing projects to error/interrupted state
        try:
            self.conn.execute(
                "UPDATE projects SET status = 'error', error_message = 'Processing interrupted (App restarted)' WHERE status = 'processing'"
            )
            self.conn.commit()
        except Exception:
            pass

        # Update pause button visual state based on persisted setting
        if self.worker.is_paused():
            self._pause_btn.configure(text="▶ Resume Queue", bootstyle="success-outline")
        else:
            self._pause_btn.configure(text="⏸ Pause Queue", bootstyle="warning-outline")

        watch_folder = db.get_setting(self.conn, "watch_folder", "").strip()
        if watch_folder and watch_folder != "Not set" and os.path.isdir(watch_folder):
            self.watcher.scan_existing(watch_folder)
            self.watcher.start(watch_folder)
        self.worker.start()

    def _toggle_queue_pause(self):
        if self.worker.is_paused():
            self.worker.resume()
            self._pause_btn.configure(text="⏸ Pause Queue", bootstyle="warning-outline")
        else:
            self.worker.pause()
            self._pause_btn.configure(text="▶ Resume Queue", bootstyle="success-outline")

    def _restart_watcher(self):
        watch_folder = db.get_setting(self.conn, "watch_folder", "").strip()
        self.watcher.stop()
        if watch_folder and watch_folder != "Not set" and os.path.isdir(watch_folder):
            self.watcher.scan_existing(watch_folder)
            self.watcher.start(watch_folder)
        self._refresh_list()

    def _on_new_project(self, pid: int):
        """Called from watcher thread when a new project is detected."""
        self.after(0, self._refresh_list)

    def _on_pipeline_update(self, project_id: int, step: int,
                            status: str, msg: str):
        """Called from pipeline worker thread."""
        self.after(0, self._refresh_list)
        if self.selected_project_id == project_id:
            self.after(0, lambda: self._select_project(project_id))

    def _periodic_refresh(self):
        """Periodically refresh the project list for processing updates."""
        self._refresh_list()
        # Re-render the detail panel if viewing a processing project
        if self.selected_project_id:
            project = db.get_project(self.conn, self.selected_project_id)
            if project and project["status"] == "processing":
                self._show_project_detail(project)
        self.after(3000, self._periodic_refresh)

    def _on_close(self):
        self._stop_player()
        self.watcher.stop()
        self.worker.stop()
        self.conn.close()
        self.destroy()
