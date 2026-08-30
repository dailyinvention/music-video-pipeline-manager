"""
Main GUI application for the Music Video Pipeline Manager.

Built with ttkbootstrap for a modern dark-themed desktop experience.
"""

import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
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
from pipeline.frame_extractor import (
    analyze_interesting_frames,
    extract_thumbnail_image,
    export_frame,
    batch_export_frames,
    extract_full_frame,
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
    "pending_deployment": "📋",
    "deploying": "🚀",
    "done": "✅",
    "error": "❌",
}

STATUS_COLORS = {
    "staged": "info",
    "queued": "warning",
    "processing": "primary",
    "pending_deployment": "info",
    "deploying": "primary",
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
        self.watcher = FolderWatcher(DB_PATH, on_new_project=self._on_new_project)
        self.worker = PipelineWorker(DB_PATH, on_update=self._on_pipeline_update)

        # State
        self.selected_project_id: int | None = None
        self._player: VideoPlayer | None = None
        self._loop_thumbnails: dict[int, ImageTk.PhotoImage] = {}
        self._interesting_img_thumbnails: dict[str, ImageTk.PhotoImage] = {}
        self._current_interesting_candidates: list[dict] = []
        self._interesting_cand_vars: dict[int, tk.BooleanVar] = {}
        self._re_render_all_templates_callback = None

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

        ttk.Button(
            top, text="🖼️ Extract Images", bootstyle="info-outline",
            command=self._open_standalone_frame_extractor,
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
            list_frame, columns=("reorder",), show="tree",
            selectmode="browse", style="dark.Treeview",
        )
        self._tree.column("#0", width=220)
        self._tree.column("reorder", width=46, anchor=CENTER, stretch=False)
        self._tree.pack(fill=BOTH, expand=True, side=LEFT)

        tree_scroll = ttk.Scrollbar(
            list_frame, orient=VERTICAL, command=self._tree.yview,
        )
        tree_scroll.pack(fill=Y, side=RIGHT)
        self._tree.configure(yscrollcommand=tree_scroll.set)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<ButtonRelease-1>", self._on_tree_click)

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
        self._re_render_all_templates_callback = None

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

    def _check_and_update_overall_done(self, pid: int):
        project = db.get_project(self.conn, pid)
        if not project:
            return
        
        folder = project["folder_path"]
        name = project["name"]
        fb_video_path = os.path.join(folder, f"{name} 4k - Facebook.mp4")
        yt_video_path = os.path.join(folder, f"{name} 4k (Video).mp4")
        short_video_path = os.path.join(folder, f"{name} - YouTube Short.mp4")

        need_fb = (
            bool(project.get("process_facebook") if project.get("process_facebook") is not None else 1) or
            os.path.isfile(fb_video_path) or
            project.get("fb_upload_status") == "done"
        )
        need_yt = (
            bool(project.get("process_youtube") if project.get("process_youtube") is not None else 1) or
            os.path.isfile(yt_video_path) or
            project.get("yt_upload_status") == "done"
        )
        need_short = (
            (bool(project.get("process_youtube") if project.get("process_youtube") is not None else 1) and not bool(project.get("skip_shorts") if project.get("skip_shorts") is not None else 0)) or
            os.path.isfile(short_video_path) or
            project.get("short_upload_status") == "done"
        )
        need_tiktok = (
            bool(project.get("process_tiktok") if project.get("process_tiktok") is not None else 0) or
            project.get("tiktok_upload_status") == "done"
        )
        
        fb_ok = not need_fb or (project.get("fb_upload_status") == "done")
        yt_ok = not need_yt or (project.get("yt_upload_status") == "done")
        short_ok = not need_short or (project.get("short_upload_status") == "done")
        tiktok_ok = not need_tiktok or (project.get("tiktok_upload_status") == "done")
        
        if fb_ok and yt_ok and short_ok and tiktok_ok:
            db.update_project(self.conn, pid, status="done", error_message="")
        else:
            if project.get("status") == "done":
                db.update_project(self.conn, pid, status="pending_deployment")

    def _show_project_detail(self, project: dict):
        # Preserve active tab and scroll position if reloading the same project
        active_tab_idx = None
        scroll_pos = None
        
        old_pid = getattr(self, "_current_detail_project_id", None)
        if old_pid == project["id"]:
            if hasattr(self, "_detail_notebook") and self._detail_notebook and self._detail_notebook.winfo_exists():
                try:
                    active_tab_idx = self._detail_notebook.index(self._detail_notebook.select())
                except Exception:
                    pass
            if hasattr(self, "_detail_content") and self._detail_content and self._detail_content.winfo_exists():
                try:
                    if hasattr(self._detail_content, "vscroll"):
                        scroll_pos = self._detail_content.vscroll.get()[0]
                except Exception:
                    pass

        self._clear_detail()

        scroll = ScrolledFrame(self._detail_frame, autohide=True)
        scroll.pack(fill=BOTH, expand=True)
        self._detail_content = scroll

        f = ttk.Frame(scroll, padding=(0, 0, 20, 0))
        f.pack(fill=BOTH, expand=True)

        status = project["status"]
        self._current_detail_project_status = status
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

        # Error / last-run message (show on any status if present)
        if project.get("error_message"):
            err_msg = project["error_message"]
            if len(err_msg) > 1000:
                err_msg = err_msg[:1000] + "\n... (truncated, see full log below)"
            err_frame = ttk.Frame(f)
            err_frame.pack(fill=X, pady=(0, 10))
            ttk.Label(
                err_frame,
                text=f"Last error: {err_msg}",
                bootstyle="danger",
                wraplength=700,
            ).pack(fill=X)
            if status == "error":
                ttk.Button(
                    err_frame, text="Retry",
                    bootstyle="warning",
                    command=lambda: self._queue_project(pid),
                ).pack(anchor=E, pady=5)

        # Status-specific panels packed at the top
        if status == "pending_link":
            self._show_pending_link_panel(f, project)
        elif status in ("processing", "deploying"):
            self._show_processing_progress_panel(f, project)
        elif status == "done":
            self._show_done_summary_panel(f, project)

        # ---- Editable fields (staged / queued / error / pending_deployment / done) ----
        is_editable = status in ("staged", "error", "queued", "pending_deployment", "done")

        # Folder path
        ttk.Label(
            f, text=f"📁 {project['folder_path']}",
            font=("Helvetica", 9), bootstyle="secondary",
        ).pack(anchor=W, pady=(0, 10))

        # Create Notebook for tabs
        notebook = ttk.Notebook(f)
        notebook.pack(fill=BOTH, expand=True, pady=10)
        self._detail_notebook = notebook

        # Tab 1: Video Files & Loops
        tab_files = ttk.Frame(notebook, padding=10)
        notebook.add(tab_files, text="🎬 Files & Loops")

        # Tab 2: Shorts Text Overlay
        tab_shorts = ttk.Frame(notebook, padding=10)
        notebook.add(tab_shorts, text="📱 Shorts Overlay")

        # Tab 3: Interesting Images
        tab_images = ttk.Frame(notebook, padding=10)
        notebook.add(tab_images, text="🖼️ Interesting Images")
        self._tab_images = tab_images

        # Tab 4: Publishing & Scheduling
        tab_pub = ttk.Frame(notebook, padding=10)
        notebook.add(tab_pub, text="🚀 Publishing & Scheduling")
        self._tab_pub = tab_pub

        # ------------------ TAB 1: FILES & LOOPS ------------------
        # Facebook Video (Short)
        fb_vid_frame = ttk.Frame(tab_files)
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
        fb_aud_frame = ttk.Frame(tab_files)
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
        yt_vid_frame = ttk.Frame(tab_files)
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
        aud_frame = ttk.Frame(tab_files)
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
        settings_frame = ttk.Frame(tab_files)
        settings_frame.pack(fill=X, pady=10)

        self._proc_fb_var = tk.BooleanVar(value=bool(project.get("process_facebook") if project.get("process_facebook") is not None else 1))
        self._proc_fb_chk = ttk.Checkbutton(
            settings_frame, text="Process FB Video",
            variable=self._proc_fb_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_fb_chk.pack(side=LEFT, padx=(0, 15))

        self._proc_yt_var = tk.BooleanVar(value=bool(project.get("process_youtube") if project.get("process_youtube") is not None else 1))
        self._proc_yt_chk = ttk.Checkbutton(
            settings_frame, text="Process YT Video",
            variable=self._proc_yt_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_yt_chk.pack(side=LEFT, padx=(0, 15))

        self._proc_shorts_var = tk.BooleanVar(value=not bool(project.get("skip_shorts") if project.get("skip_shorts") is not None else 0))
        self._proc_shorts_chk = ttk.Checkbutton(
            settings_frame, text="Process YT Short",
            variable=self._proc_shorts_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_shorts_chk.pack(side=LEFT, padx=(0, 15))

        self._neg_logo_var = tk.BooleanVar(value=bool(project.get("negative_logo", 0)))
        self._neg_logo_chk = ttk.Checkbutton(
            settings_frame, text="Negative Logo",
            variable=self._neg_logo_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._neg_logo_chk.pack(side=LEFT, padx=(0, 15))

        self._short_src_fb_var = tk.BooleanVar(value=bool(project.get("short_source_fb", 1)))
        self._short_src_fb_chk = ttk.Checkbutton(
            settings_frame, text="Use FB Video for Short",
            variable=self._short_src_fb_var,
            state="normal" if is_editable else "disabled",
            bootstyle="warning-square-toggle"
        )
        self._short_src_fb_chk.pack(side=LEFT, padx=(0, 15))

        settings_frame_2 = ttk.Frame(tab_files)
        settings_frame_2.pack(fill=X, pady=(0, 10))

        self._proc_tiktok_var = tk.BooleanVar(value=bool(project.get("process_tiktok") if project.get("process_tiktok") is not None else 0))
        self._proc_tiktok_chk = ttk.Checkbutton(
            settings_frame_2, text="Process TikTok Short",
            variable=self._proc_tiktok_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_tiktok_chk.pack(side=LEFT, padx=(0, 15))

        self._proc_canvas_var = tk.BooleanVar(value=bool(project.get("generate_spotify_canvas", 0)))
        self._proc_canvas_chk = ttk.Checkbutton(
            settings_frame_2, text="Process Spotify Canvas",
            variable=self._proc_canvas_var,
            state="normal" if is_editable else "disabled",
            bootstyle="success-square-toggle"
        )
        self._proc_canvas_chk.pack(side=LEFT)

        # Separator
        ttk.Separator(tab_files).pack(fill=X, pady=10)

        # Loop Section Header
        loop_header = ttk.Frame(tab_files)
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
        loop_params = ttk.Frame(tab_files)
        loop_params.pack(fill=X, pady=5)

        ttk.Label(loop_params, text="Fade (sec):").pack(side=LEFT)
        self._loop_fade_var = tk.DoubleVar(value=project.get("loop_fade", 2.0))
        ttk.Spinbox(
            loop_params, from_=0, to=10, increment=0.5,
            textvariable=self._loop_fade_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 20))

        ttk.Label(loop_params, text="Selected Loop:").pack(side=LEFT)
        self._loop_pick_var = tk.IntVar(value=project.get("loop_pick", 1))
        self._loop_pick_spin = ttk.Spinbox(
            loop_params, from_=1, to=10,
            textvariable=self._loop_pick_var, width=4,
            state="normal" if is_editable else "readonly",
        )
        self._loop_pick_spin.pack(side=LEFT, padx=5)

        # Loop limits (min/max duration)
        loop_limits = ttk.Frame(tab_files)
        loop_limits.pack(fill=X, pady=5)

        ttk.Label(loop_limits, text="Min Dur (s):").pack(side=LEFT)
        self._loop_min_dur_var = tk.DoubleVar(value=project.get("loop_min_dur", 10.0))
        ttk.Spinbox(
            loop_limits, from_=1.0, to=300.0, increment=5.0,
            textvariable=self._loop_min_dur_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 20))

        ttk.Label(loop_limits, text="Max Dur (s) (0=all):").pack(side=LEFT)
        self._loop_max_dur_var = tk.DoubleVar(value=project.get("loop_max_dur", 0.0))
        ttk.Spinbox(
            loop_limits, from_=0.0, to=600.0, increment=10.0,
            textvariable=self._loop_max_dur_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 20))

        # Loop candidates frame
        self._candidates_frame = ttk.Frame(tab_files)
        self._candidates_frame.pack(fill=X, pady=5)
        candidates_json = project.get("loop_candidates_json", "[]")
        try:
            candidates = json.loads(candidates_json)
        except (json.JSONDecodeError, TypeError):
            candidates = []
        if candidates:
            self._show_candidates(candidates, is_editable)

        # Spotify Canvas Section Header
        ttk.Separator(tab_files).pack(fill=X, pady=10)
        canvas_header = ttk.Frame(tab_files)
        canvas_header.pack(fill=X, pady=(0, 5))
        ttk.Label(
            canvas_header, text="Spotify Canvas Selection",
            font=("Helvetica", 11, "bold"),
        ).pack(side=LEFT)

        if is_editable:
            self._analyze_canvas_btn = ttk.Button(
                canvas_header, text="▶ Analyze Canvas",
                bootstyle="info",
                command=lambda: self._analyze_canvas(pid),
            )
            self._analyze_canvas_btn.pack(side=RIGHT)

        canvas_params = ttk.Frame(tab_files)
        canvas_params.pack(fill=X, pady=5)
        ttk.Label(canvas_params, text="Selected Canvas:").pack(side=LEFT)
        self._canvas_pick_var = tk.IntVar(value=project.get("spotify_canvas_pick", 1))
        self._canvas_pick_spin = ttk.Spinbox(
            canvas_params, from_=1, to=10,
            textvariable=self._canvas_pick_var, width=4,
            state="normal" if is_editable else "disabled",
        )
        self._canvas_pick_spin.pack(side=LEFT, padx=5)

        self._canvas_candidates_frame = ttk.Frame(tab_files)
        self._canvas_candidates_frame.pack(fill=X, pady=5)
        canvas_cands_json = project.get("spotify_candidates_json", "[]")
        try:
            canvas_cands = json.loads(canvas_cands_json)
        except (json.JSONDecodeError, TypeError):
            canvas_cands = []
        if canvas_cands:
            self._show_canvas_candidates(canvas_cands, is_editable)

        # ------------------ TAB 2: SHORTS TEXT OVERLAY ------------------
        ttk.Label(
            tab_shorts, text="YouTube Short Text:",
            font=("Helvetica", 11, "bold"),
        ).pack(anchor=W, pady=(0, 5))

        self._text_widget = tk.Text(
            tab_shorts, height=4, wrap="word",
            bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0",
            font=("Helvetica", 11),
        )
        self._text_widget.pack(fill=X, pady=(0, 10))
        self._text_widget.insert("1.0", project.get("overlay_text", ""))
        if not is_editable:
            self._text_widget.configure(state="disabled")

        # Text params row
        params = ttk.Frame(tab_shorts)
        params.pack(fill=X, pady=5)

        ttk.Label(params, text="Font Size:").pack(side=LEFT)
        self._fontsize_var = tk.IntVar(value=project.get("font_size", 90))
        ttk.Spinbox(
            params, from_=20, to=200,
            textvariable=self._fontsize_var, width=6,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(5, 15))

        ttk.Label(params, text="Fade In:").pack(side=LEFT)
        self._fi_start_var = tk.DoubleVar(value=project.get("fade_in_start", 2.0))
        ttk.Spinbox(
            params, from_=0, to=30, increment=0.5,
            textvariable=self._fi_start_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)
        ttk.Label(params, text="→").pack(side=LEFT)
        self._fi_end_var = tk.DoubleVar(value=project.get("fade_in_end", 4.0))
        ttk.Spinbox(
            params, from_=0, to=30, increment=0.5,
            textvariable=self._fi_end_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=(2, 15))

        ttk.Label(params, text="Fade Out:").pack(side=LEFT)
        self._fo_start_var = tk.DoubleVar(value=project.get("fade_out_start", 8.0))
        ttk.Spinbox(
            params, from_=0, to=60, increment=0.5,
            textvariable=self._fo_start_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)
        ttk.Label(params, text="→").pack(side=LEFT)
        self._fo_end_var = tk.DoubleVar(value=project.get("fade_out_end", 10.0))
        ttk.Spinbox(
            params, from_=0, to=60, increment=0.5,
            textvariable=self._fo_end_var, width=5,
            state="normal" if is_editable else "readonly",
        ).pack(side=LEFT, padx=2)


        # ------------------ TAB 3: INTERESTING IMAGES ------------------
        self._build_interesting_images_tab(tab_images, project)

        # ------------------ TAB 4: PUBLISHING & SCHEDULING ------------------
        # 0. Video Player Preview Frame
        player_frame = ttk.LabelFrame(tab_pub, text="Embedded Video Player Preview")
        player_frame.pack(fill=X, pady=(0, 10))
        player_pad = ttk.Frame(player_frame, padding=10)
        player_pad.pack(fill=X)

        self._player_canvas = tk.Canvas(player_pad, bg="#0d0d1a", highlightthickness=0, height=300)
        self._player_canvas.pack(fill=X)

        player_controls = ttk.Frame(player_pad)
        player_controls.pack(fill=X, pady=5)

        self._play_btn = ttk.Button(player_controls, text="▶ Play", bootstyle="success", command=self._toggle_play)
        self._play_btn.pack(side=LEFT, padx=5)

        self._stop_btn = ttk.Button(player_controls, text="⏹ Stop", bootstyle="danger-outline", command=self._stop_player)
        self._stop_btn.pack(side=LEFT)

        self._player_status = ttk.Label(player_controls, text="", bootstyle="secondary")
        self._player_status.pack(side=LEFT, padx=10)

        self._player = VideoPlayer(self._player_canvas)

        # 1. Output Files Previews (for playing rendered files)
        previews_lf = ttk.LabelFrame(tab_pub, text="Generated Video Previews")
        previews_lf.pack(fill=X, pady=(0, 10))
        previews_pad = ttk.Frame(previews_lf, padding=10)
        previews_pad.pack(fill=X)

        fb_out_path = os.path.join(project["folder_path"], f"{name} 4k - Facebook.mp4")
        yt_out_path = os.path.join(project["folder_path"], f"{name} 4k (Video).mp4")
        short_out_path = os.path.join(project["folder_path"], f"{name} - YouTube Short.mp4")

        previews_exist = False
        for label, path in [("Facebook Video", fb_out_path), ("YouTube Long Video", yt_out_path), ("YouTube Short Video", short_out_path)]:
            row = ttk.Frame(previews_pad)
            row.pack(fill=X, pady=3)
            ttk.Label(row, text=f"{label}:", width=20, font=("Helvetica", 10, "bold")).pack(side=LEFT)
            if os.path.isfile(path):
                previews_exist = True
                size_mb = os.path.getsize(path) / (1024 * 1024)
                ttk.Label(row, text=f"{os.path.basename(path)} ({size_mb:.1f} MB)", bootstyle="secondary").pack(side=LEFT)
                ttk.Button(
                    row, text="▶ Play", bootstyle="success-outline", width=6,
                    command=lambda p=path: self._play_preview(p)
                ).pack(side=RIGHT)
            else:
                ttk.Label(row, text="File not generated / not found", bootstyle="warning").pack(side=LEFT)

        # Prepopulate default templates if empty
        from pipeline.upload import render_template
        
        desc_body = project.get("description_body", "") or project.get("overlay_text", "")
        overlay_val = project.get("overlay_text", "")
        
        main_yt_id = ""
        for s in db.get_steps(self.conn, pid):
            if s["step_num"] == 11 and s.get("output_file") and not s["output_file"].startswith("Bypassed"):
                main_yt_id = s["output_file"]
                break
        yt_url_val = f"https://www.youtube.com/watch?v={main_yt_id}" if main_yt_id else ""
        
        fb_body_val = project.get("fb_post_body", "") or ""
        if not fb_body_val:
            fb_template = db.get_setting(self.conn, "fb_post_template", "{{title}}\n\n{{body}}")
            fb_body_val = render_template(fb_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)
            
        yt_title_val = project.get("yt_title_body", "") or ""
        if not yt_title_val:
            yt_title_template = db.get_setting(self.conn, "yt_title_template", "{{title}}")
            yt_title_val = render_template(yt_title_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)
            
        yt_desc_val = project.get("yt_description_body", "") or ""
        if not yt_desc_val:
            yt_desc_template = db.get_setting(self.conn, "yt_desc_template", "{{body}}")
            yt_desc_val = render_template(yt_desc_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)
            
        short_title_val = project.get("short_title_body", "") or ""
        if not short_title_val:
            short_title_template = db.get_setting(self.conn, "short_title_template", "{{title}} #shorts")
            short_title_val = render_template(short_title_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)
            
        short_desc_val = project.get("short_description_body", "") or ""
        if not short_desc_val:
            short_desc_template = db.get_setting(self.conn, "short_desc_template", "Relaxing music to sooth the soul and help guide you to sleep.\n\nChannel:  @music_to_sleep_to  \nVisit here to view the full-length 4k Youtube video: {{youtube-url}}\n\n© {{year}} Music To Sleep To. All Rights Reserved.\nMade with the help of Suno.")
            short_desc_val = render_template(short_desc_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)

        tiktok_title_val = project.get("tiktok_title_body", "") or ""
        if not tiktok_title_val:
            tiktok_title_template = db.get_setting(self.conn, "tiktok_title_template", "{{title}} #shorts #fyp #foryou")
            tiktok_title_val = render_template(tiktok_title_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)

        tiktok_desc_val = project.get("tiktok_description_body", "") or ""
        if not tiktok_desc_val:
            tiktok_desc_template = db.get_setting(self.conn, "tiktok_desc_template", "{{body}}\n\n#music #sleep #relaxing #fyp #foryou")
            tiktok_desc_val = render_template(tiktok_desc_template, desc_body, project.get("name", ""), yt_url=yt_url_val, overlay_text=overlay_val)

        tags_val = project.get("tags", "") or ""
        if not tags_val:
            tags_val = db.get_setting(self.conn, "default_tags", "")
            
        # 1. Raw Description / Quote Body Section
        desc_lf = ttk.LabelFrame(tab_pub, text="Description / Quote Body ({{body}})")
        desc_lf.pack(fill=X, pady=5)
        desc_pad = ttk.Frame(desc_lf, padding=10)
        desc_pad.pack(fill=X)
        
        self._description_body_text = tk.Text(desc_pad, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        self._description_body_text.pack(fill=X, pady=3)
        self._description_body_text.insert("1.0", desc_body)
        if not is_editable:
            self._description_body_text.configure(state="disabled")
            
        def re_render_all_templates(show_info=True):
            current_desc = self._description_body_text.get("1.0", "end-1c").strip()
            
            fb_template = db.get_setting(self.conn, "fb_post_template", "{{title}}\n\n{{body}}")
            yt_title_template = db.get_setting(self.conn, "yt_title_template", "{{title}}")
            yt_desc_template = db.get_setting(self.conn, "yt_desc_template", "{{body}}")
            short_title_template = db.get_setting(self.conn, "short_title_template", "{{title}} #shorts")
            short_desc_template = db.get_setting(self.conn, "short_desc_template", "Relaxing music to sooth the soul and help guide you to sleep.\n\nChannel:  @music_to_sleep_to  \nVisit here to view the full-length 4k Youtube video: {{youtube-url}}\n\n© {{year}} Music To Sleep To. All Rights Reserved.\nMade with the help of Suno.")
            tiktok_title_template = db.get_setting(self.conn, "tiktok_title_template", "{{title}} #shorts #fyp #foryou")
            tiktok_desc_template = db.get_setting(self.conn, "tiktok_desc_template", "{{body}}\n\n#music #sleep #relaxing #fyp #foryou")
            
            current_yt_id = ""
            for s in db.get_steps(self.conn, pid):
                if s["step_num"] == 11 and s.get("output_file") and not s["output_file"].startswith("Bypassed"):
                    current_yt_id = s["output_file"]
                    break
            current_yt_url = f"https://www.youtube.com/watch?v={current_yt_id}" if current_yt_id else ""
            current_overlay = project.get("overlay_text", "")

            fb_v = render_template(fb_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            yt_t = render_template(yt_title_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            yt_d = render_template(yt_desc_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            st_t = render_template(short_title_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            st_d = render_template(short_desc_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            tt_t = render_template(tiktok_title_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            tt_d = render_template(tiktok_desc_template, current_desc, name, yt_url=current_yt_url, overlay_text=current_overlay)
            
            self._fb_post_body_text.configure(state="normal")
            self._fb_post_body_text.delete("1.0", tk.END)
            self._fb_post_body_text.insert("1.0", fb_v)
            if not is_editable:
                self._fb_post_body_text.configure(state="disabled")
                
            self._yt_title_body_var.set(yt_t)
            
            self._yt_desc_body_text.configure(state="normal")
            self._yt_desc_body_text.delete("1.0", tk.END)
            self._yt_desc_body_text.insert("1.0", yt_d)
            if not is_editable:
                self._yt_desc_body_text.configure(state="disabled")
                
            self._short_title_body_var.set(st_t)
            
            self._short_desc_body_text.configure(state="normal")
            self._short_desc_body_text.delete("1.0", tk.END)
            self._short_desc_body_text.insert("1.0", st_d)
            if not is_editable:
                self._short_desc_body_text.configure(state="disabled")

            if hasattr(self, "_tiktok_title_body_var") and self._tiktok_title_body_var:
                self._tiktok_title_body_var.set(tt_t)
            if hasattr(self, "_tiktok_desc_body_text") and self._tiktok_desc_body_text:
                self._tiktok_desc_body_text.configure(state="normal")
                self._tiktok_desc_body_text.delete("1.0", tk.END)
                self._tiktok_desc_body_text.insert("1.0", tt_d)
                if not is_editable:
                    self._tiktok_desc_body_text.configure(state="disabled")
                
            if show_info:
                messagebox.showinfo("Success", "Templates successfully re-applied using current quote body!")
                
        self._re_render_all_templates_callback = re_render_all_templates
        
        if is_editable:
            # Bind key release and paste events for fully automatic real-time updates
            self._description_body_text.bind("<KeyRelease>", lambda event: re_render_all_templates(show_info=False))
            self._description_body_text.bind("<<Paste>>", lambda event: self.after(10, lambda: re_render_all_templates(show_info=False)))
            
            btn_frame = ttk.Frame(desc_pad)
            btn_frame.pack(fill=X, pady=5)
            
            self._gemini_quote_btn = ttk.Button(
                btn_frame, text="✨ Gemini Quote",
                bootstyle="primary",
                command=lambda: self._generate_gemini_quote(pid),
            )
            self._gemini_quote_btn.pack(side=LEFT, padx=(0, 5))
            
            re_render_btn = ttk.Button(
                btn_frame, text="🔄 Reset Templates",
                bootstyle="secondary-outline",
                command=lambda: re_render_all_templates(show_info=True),
            )
            re_render_btn.pack(side=RIGHT)
            
        # Tags Section
        tags_lf = ttk.LabelFrame(tab_pub, text="Tags (comma-separated)")
        tags_lf.pack(fill=X, pady=5)
        tags_pad = ttk.Frame(tags_lf, padding=10)
        tags_pad.pack(fill=X)
        
        self._tags_entry_var = tk.StringVar(value=tags_val)
        tags_entry = ttk.Entry(
            tags_pad, textvariable=self._tags_entry_var,
            state="normal" if is_editable else "readonly"
        )
        tags_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))
        
        if is_editable:
            self._gemini_tags_btn = ttk.Button(
                tags_pad, text="✨ Gemini Tags",
                bootstyle="primary-outline",
                command=lambda: self._generate_gemini_tags(pid)
            )
            self._gemini_tags_btn.pack(side=RIGHT)
            
        # 2. Facebook Section
        fb_lf = ttk.LabelFrame(tab_pub, text="Facebook Post Details")
        fb_lf.pack(fill=X, pady=5)
        fb_pad = ttk.Frame(fb_lf, padding=10)
        fb_pad.pack(fill=X)
        
        # Row with Checkbox, Status, and Toggle Button
        fb_ctrl_frame = ttk.Frame(fb_pad)
        fb_ctrl_frame.pack(fill=X, pady=(0, 10))
        
        self._deploy_fb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(fb_ctrl_frame, text="Deploy Facebook", variable=self._deploy_fb_var, state="normal" if is_editable else "disabled").pack(side=LEFT)
        
        fb_status = project.get("fb_upload_status", "pending")
        fb_status_color = "info" if fb_status == "pending" else "success" if fb_status == "done" else "danger"
        ttk.Label(fb_ctrl_frame, text=f" [{fb_status.upper()}] ", bootstyle=f"inverse-{fb_status_color}").pack(side=LEFT, padx=10)
        
        def toggle_fb_status():
            new_status = "pending" if fb_status == "done" else "done"
            db.update_project(self.conn, pid, fb_upload_status=new_status)
            self._check_and_update_overall_done(pid)
            self._select_project(pid)
            
        toggle_text = "Mark Pending" if fb_status == "done" else "Mark Deployed"
        toggle_style = "secondary-outline" if fb_status == "done" else "success-outline"
        ttk.Button(fb_ctrl_frame, text=toggle_text, bootstyle=toggle_style, command=toggle_fb_status).pack(side=RIGHT)
        
        ttk.Label(fb_pad, text="Post Text (Body):").pack(anchor=W)
        self._fb_post_body_text = tk.Text(fb_pad, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        self._fb_post_body_text.pack(fill=X, pady=3)
        self._fb_post_body_text.insert("1.0", fb_body_val)
        if not is_editable:
            self._fb_post_body_text.configure(state="disabled")

        fb_sched_frame = ttk.Frame(fb_pad)
        fb_sched_frame.pack(fill=X, pady=5)
        ttk.Label(fb_sched_frame, text="Schedule Time:").pack(side=LEFT)
        
        fb_time_val = project.get("fb_schedule_time", "")
        fb_date_val, fb_time_only = "", ""
        if fb_time_val:
            try:
                parts = fb_time_val.replace("T", " ").split(" ")
                fb_date_val = parts[0]
                fb_time_only = parts[1][:5]
            except Exception:
                pass
        self._fb_date_entry = ttk.DateEntry(fb_sched_frame, bootstyle="info", width=12, dateformat="%Y-%m-%d")
        self._fb_date_entry.pack(side=LEFT, padx=5)
        if fb_date_val:
            try:
                self._fb_date_entry.entry.delete(0, tk.END)
                self._fb_date_entry.entry.insert(0, fb_date_val)
            except Exception:
                pass
        else:
            try:
                self._fb_date_entry.entry.delete(0, tk.END)
            except Exception:
                pass
        self._fb_time_var = tk.StringVar(value=fb_time_only or "21:00")
        self._fb_time_entry = ttk.Entry(fb_sched_frame, textvariable=self._fb_time_var, width=6, state="normal" if is_editable else "readonly")
        self._fb_time_entry.pack(side=LEFT)
        ttk.Label(fb_sched_frame, text=" (HH:MM - Local Time)", bootstyle="secondary").pack(side=LEFT, padx=3)

        def _autofill_fb_date():
            from pipeline.upload import get_last_facebook_scheduled_date
            import datetime as dt_mod
            fb_page_id = db.get_setting(self.conn, "fb_page_id", "").strip()
            fb_token = db.get_setting(self.conn, "fb_page_token", "").strip()
            if not fb_token:
                fb_token = db.get_setting(self.conn, "fb_long_user_token", "").strip()
            if not fb_page_id or not fb_token:
                messagebox.showerror("Missing Credentials", "Facebook Page ID and Page Access Token are required in Settings.")
                return
            last_dt, err = get_last_facebook_scheduled_date(fb_page_id, fb_token)
            if err or not last_dt:
                messagebox.showerror("Could Not Fetch", f"Could not retrieve scheduled videos from Facebook:\n{err}")
                return
            next_dt = last_dt + dt_mod.timedelta(days=1)
            try:
                self._fb_date_entry.entry.delete(0, tk.END)
                self._fb_date_entry.entry.insert(0, next_dt.strftime("%Y-%m-%d"))
            except Exception:
                pass

        ttk.Button(
            fb_sched_frame, text="📅 Auto-fill from FB",
            bootstyle="info-outline",
            command=_autofill_fb_date,
        ).pack(side=LEFT, padx=(10, 0))

        # 3. YouTube Long Section
        yt_lf = ttk.LabelFrame(tab_pub, text="YouTube Video Details")
        yt_lf.pack(fill=X, pady=5)
        yt_pad = ttk.Frame(yt_lf, padding=10)
        yt_pad.pack(fill=X)
        
        # Row with Checkbox, Status, and Toggle Button
        yt_ctrl_frame = ttk.Frame(yt_pad)
        yt_ctrl_frame.pack(fill=X, pady=(0, 10))
        
        self._deploy_yt_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(yt_ctrl_frame, text="Deploy YouTube Long", variable=self._deploy_yt_var, state="normal" if is_editable else "disabled").pack(side=LEFT)
        
        yt_status = project.get("yt_upload_status", "pending")
        yt_status_color = "info" if yt_status == "pending" else "success" if yt_status == "done" else "danger"
        ttk.Label(yt_ctrl_frame, text=f" [{yt_status.upper()}] ", bootstyle=f"inverse-{yt_status_color}").pack(side=LEFT, padx=10)
        
        def toggle_yt_status():
            new_status = "pending" if yt_status == "done" else "done"
            db.update_project(self.conn, pid, yt_upload_status=new_status)
            self._check_and_update_overall_done(pid)
            self._select_project(pid)
            
        toggle_text = "Mark Pending" if yt_status == "done" else "Mark Deployed"
        toggle_style = "secondary-outline" if yt_status == "done" else "success-outline"
        ttk.Button(yt_ctrl_frame, text=toggle_text, bootstyle=toggle_style, command=toggle_yt_status).pack(side=RIGHT)
        
        yt_title_frame = ttk.Frame(yt_pad)
        yt_title_frame.pack(fill=X, pady=3)
        ttk.Label(yt_title_frame, text="Title Body:", width=15).pack(side=LEFT)
        self._yt_title_body_var = tk.StringVar(value=yt_title_val)
        self._yt_title_body_entry = ttk.Entry(yt_title_frame, textvariable=self._yt_title_body_var, state="normal" if is_editable else "readonly")
        self._yt_title_body_entry.pack(side=LEFT, fill=X, expand=True)

        ttk.Label(yt_pad, text="Description Body:").pack(anchor=W, pady=(5, 0))
        self._yt_desc_body_text = tk.Text(yt_pad, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        self._yt_desc_body_text.pack(fill=X, pady=3)
        self._yt_desc_body_text.insert("1.0", yt_desc_val)
        if not is_editable:
            self._yt_desc_body_text.configure(state="disabled")

        yt_sched_frame = ttk.Frame(yt_pad)
        yt_sched_frame.pack(fill=X, pady=5)
        ttk.Label(yt_sched_frame, text="Schedule Time:").pack(side=LEFT)
        
        yt_time_val = project.get("yt_schedule_time", "")
        yt_date_val, yt_time_only = "", ""
        if yt_time_val:
            try:
                parts = yt_time_val.replace("T", " ").split(" ")
                yt_date_val = parts[0]
                yt_time_only = parts[1][:5]
            except Exception:
                pass
        self._yt_date_entry = ttk.DateEntry(yt_sched_frame, bootstyle="info", width=12, dateformat="%Y-%m-%d")
        self._yt_date_entry.pack(side=LEFT, padx=5)
        if yt_date_val:
            try:
                self._yt_date_entry.entry.delete(0, tk.END)
                self._yt_date_entry.entry.insert(0, yt_date_val)
            except Exception:
                pass
        else:
            try:
                self._yt_date_entry.entry.delete(0, tk.END)
            except Exception:
                pass
        self._yt_time_var = tk.StringVar(value=yt_time_only or "21:00")
        self._yt_time_entry = ttk.Entry(yt_sched_frame, textvariable=self._yt_time_var, width=6, state="normal" if is_editable else "readonly")
        self._yt_time_entry.pack(side=LEFT)
        ttk.Label(yt_sched_frame, text=" (HH:MM - Local Time)", bootstyle="secondary").pack(side=LEFT, padx=3)

        def _autofill_yt_long_date():
            import datetime as dt_mod
            from pipeline.upload import get_youtube_client, get_last_youtube_scheduled_dates
            creds, err = get_youtube_client(self.conn)
            if not creds:
                messagebox.showerror("YouTube Not Authorized", err)
                return
            last_long, _, err = get_last_youtube_scheduled_dates(creds)
            if err:
                messagebox.showerror("Could Not Fetch", f"Could not retrieve YouTube scheduled videos:\n{err}")
                return
            if not last_long:
                messagebox.showinfo("No Videos Found", "No scheduled or published YouTube long videos found.")
                return
            weekday = last_long.weekday()  # 0=Mon … 6=Sun
            if weekday == 1:    # Tuesday → Friday same week
                next_dt = last_long + dt_mod.timedelta(days=3)
            elif weekday == 4:  # Friday → Tuesday next week
                next_dt = last_long + dt_mod.timedelta(days=4)
            else:
                day_name = last_long.strftime("%A")
                messagebox.showwarning("Unexpected Day", f"Last long video is on a {day_name}. Expected Tuesday or Friday.")
                return

            # Advance to the future along the Tuesday-Friday schedule if in the past
            while next_dt.date() < dt_mod.date.today():
                wd = next_dt.weekday()
                if wd == 1:    # Tuesday -> Friday
                    next_dt += dt_mod.timedelta(days=3)
                elif wd == 4:  # Friday -> Tuesday
                    next_dt += dt_mod.timedelta(days=4)
                else:
                    next_dt += dt_mod.timedelta(days=1)

            try:
                self._yt_date_entry.entry.delete(0, tk.END)
                self._yt_date_entry.entry.insert(0, next_dt.strftime("%Y-%m-%d"))
            except Exception:
                pass

        ttk.Button(
            yt_sched_frame, text="📅 Auto-fill from YT",
            bootstyle="info-outline",
            command=_autofill_yt_long_date,
        ).pack(side=LEFT, padx=(10, 0))

        # 4. YouTube Short Section
        short_lf = ttk.LabelFrame(tab_pub, text="YouTube Short Details")
        short_lf.pack(fill=X, pady=5)
        short_pad = ttk.Frame(short_lf, padding=10)
        short_pad.pack(fill=X)
        
        # Row with Checkbox, Status, and Toggle Button
        short_ctrl_frame = ttk.Frame(short_pad)
        short_ctrl_frame.pack(fill=X, pady=(0, 10))
        
        self._deploy_short_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(short_ctrl_frame, text="Deploy YouTube Short", variable=self._deploy_short_var, state="normal" if is_editable else "disabled").pack(side=LEFT)
        
        short_status = project.get("short_upload_status", "pending")
        short_status_color = "info" if short_status == "pending" else "success" if short_status == "done" else "danger"
        ttk.Label(short_ctrl_frame, text=f" [{short_status.upper()}] ", bootstyle=f"inverse-{short_status_color}").pack(side=LEFT, padx=10)
        
        def toggle_short_status():
            new_status = "pending" if short_status == "done" else "done"
            db.update_project(self.conn, pid, short_upload_status=new_status)
            self._check_and_update_overall_done(pid)
            self._select_project(pid)
            
        toggle_text = "Mark Pending" if short_status == "done" else "Mark Deployed"
        toggle_style = "secondary-outline" if short_status == "done" else "success-outline"
        ttk.Button(short_ctrl_frame, text=toggle_text, bootstyle=toggle_style, command=toggle_short_status).pack(side=RIGHT)
        
        short_title_frame = ttk.Frame(short_pad)
        short_title_frame.pack(fill=X, pady=3)
        ttk.Label(short_title_frame, text="Short Title:", width=15).pack(side=LEFT)
        self._short_title_body_var = tk.StringVar(value=short_title_val)
        self._short_title_body_entry = ttk.Entry(short_title_frame, textvariable=self._short_title_body_var, state="normal" if is_editable else "readonly")
        self._short_title_body_entry.pack(side=LEFT, fill=X, expand=True)

        ttk.Label(short_pad, text="Description Body:").pack(anchor=W, pady=(5, 0))
        self._short_desc_body_text = tk.Text(short_pad, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        self._short_desc_body_text.pack(fill=X, pady=3)
        self._short_desc_body_text.insert("1.0", short_desc_val)
        if not is_editable:
            self._short_desc_body_text.configure(state="disabled")
        
        short_sched_frame = ttk.Frame(short_pad)
        short_sched_frame.pack(fill=X, pady=5)
        ttk.Label(short_sched_frame, text="Schedule Time:").pack(side=LEFT)
        
        short_time_val = project.get("short_schedule_time", "")
        short_date_val, short_time_only = "", ""
        if short_time_val:
            try:
                parts = short_time_val.replace("T", " ").split(" ")
                short_date_val = parts[0]
                short_time_only = parts[1][:5]
            except Exception:
                pass
        self._short_date_entry = ttk.DateEntry(short_sched_frame, bootstyle="info", width=12, dateformat="%Y-%m-%d")
        self._short_date_entry.pack(side=LEFT, padx=5)
        if short_date_val:
            try:
                self._short_date_entry.entry.delete(0, tk.END)
                self._short_date_entry.entry.insert(0, short_date_val)
            except Exception:
                pass
        else:
            try:
                self._short_date_entry.entry.delete(0, tk.END)
            except Exception:
                pass
        self._short_time_var = tk.StringVar(value=short_time_only or "21:00")
        self._short_time_entry = ttk.Entry(short_sched_frame, textvariable=self._short_time_var, width=6, state="normal" if is_editable else "readonly")
        self._short_time_entry.pack(side=LEFT)
        ttk.Label(short_sched_frame, text=" (HH:MM - Local Time)", bootstyle="secondary").pack(side=LEFT, padx=3)

        def _autofill_yt_short_date():
            import datetime as dt_mod
            from pipeline.upload import get_youtube_client, get_last_youtube_scheduled_dates
            creds, err = get_youtube_client(self.conn)
            if not creds:
                messagebox.showerror("YouTube Not Authorized", err)
                return
            _, last_short, err = get_last_youtube_scheduled_dates(creds)
            if err:
                messagebox.showerror("Could Not Fetch", f"Could not retrieve YouTube scheduled videos:\n{err}")
                return
            if not last_short:
                messagebox.showinfo("No Videos Found", "No scheduled or published YouTube Shorts found.")
                return
            weekday = last_short.weekday()  # 0=Mon … 6=Sun
            if weekday == 0:    # Monday → Thursday same week
                next_dt = last_short + dt_mod.timedelta(days=3)
            elif weekday == 3:  # Thursday → Monday next week
                next_dt = last_short + dt_mod.timedelta(days=4)
            else:
                day_name = last_short.strftime("%A")
                messagebox.showwarning("Unexpected Day", f"Last Short is on a {day_name}. Expected Monday or Thursday.")
                return

            # Advance to the future along the Monday-Thursday schedule if in the past
            while next_dt.date() < dt_mod.date.today():
                wd = next_dt.weekday()
                if wd == 0:    # Monday -> Thursday
                    next_dt += dt_mod.timedelta(days=3)
                elif wd == 3:  # Thursday -> Monday
                    next_dt += dt_mod.timedelta(days=4)
                else:
                    next_dt += dt_mod.timedelta(days=1)

            try:
                self._short_date_entry.entry.delete(0, tk.END)
                self._short_date_entry.entry.insert(0, next_dt.strftime("%Y-%m-%d"))
            except Exception:
                pass

        ttk.Button(
            short_sched_frame, text="📅 Auto-fill from YT",
            bootstyle="info-outline",
            command=_autofill_yt_short_date,
        ).pack(side=LEFT, padx=(10, 0))

        short_status = project.get("short_upload_status", "pending")
        short_status_color = "info" if short_status == "pending" else "success" if short_status == "done" else "danger"

        # 5. TikTok Short Section
        tiktok_lf = ttk.LabelFrame(tab_pub, text="TikTok Video Details")
        tiktok_lf.pack(fill=X, pady=5)
        tiktok_pad = ttk.Frame(tiktok_lf, padding=10)
        tiktok_pad.pack(fill=X)
        
        # Row with Checkbox, Status, and Toggle Button
        tiktok_ctrl_frame = ttk.Frame(tiktok_pad)
        tiktok_ctrl_frame.pack(fill=X, pady=(0, 10))
        
        self._deploy_tiktok_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tiktok_ctrl_frame, text="Deploy TikTok Short", variable=self._deploy_tiktok_var, state="normal" if is_editable else "disabled").pack(side=LEFT)
        
        tiktok_status = project.get("tiktok_upload_status", "pending")
        tiktok_status_color = "info" if tiktok_status == "pending" else "success" if tiktok_status == "done" else "danger"
        ttk.Label(tiktok_ctrl_frame, text=f" [{tiktok_status.upper()}] ", bootstyle=f"inverse-{tiktok_status_color}").pack(side=LEFT, padx=10)
        
        def toggle_tiktok_status():
            new_status = "pending" if tiktok_status == "done" else "done"
            db.update_project(self.conn, pid, tiktok_upload_status=new_status)
            self._check_and_update_overall_done(pid)
            self._select_project(pid)
            
        toggle_text = "Mark Pending" if tiktok_status == "done" else "Mark Deployed"
        toggle_style = "secondary-outline" if tiktok_status == "done" else "success-outline"
        ttk.Button(tiktok_ctrl_frame, text=toggle_text, bootstyle=toggle_style, command=toggle_tiktok_status).pack(side=RIGHT)
        
        tiktok_title_frame = ttk.Frame(tiktok_pad)
        tiktok_title_frame.pack(fill=X, pady=3)
        ttk.Label(tiktok_title_frame, text="TikTok Title:", width=15).pack(side=LEFT)
        self._tiktok_title_body_var = tk.StringVar(value=tiktok_title_val)
        self._tiktok_title_body_entry = ttk.Entry(tiktok_title_frame, textvariable=self._tiktok_title_body_var, state="normal" if is_editable else "readonly")
        self._tiktok_title_body_entry.pack(side=LEFT, fill=X, expand=True)

        ttk.Label(tiktok_pad, text="Caption / Description Body:").pack(anchor=W, pady=(5, 0))
        self._tiktok_desc_body_text = tk.Text(tiktok_pad, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        self._tiktok_desc_body_text.pack(fill=X, pady=3)
        self._tiktok_desc_body_text.insert("1.0", tiktok_desc_val)
        if not is_editable:
            self._tiktok_desc_body_text.configure(state="disabled")
        
        tiktok_sched_frame = ttk.Frame(tiktok_pad)
        tiktok_sched_frame.pack(fill=X, pady=5)
        ttk.Label(tiktok_sched_frame, text="Schedule Time:").pack(side=LEFT)
        
        tiktok_time_val = project.get("tiktok_schedule_time", "")
        tiktok_date_val, tiktok_time_only = "", ""
        if tiktok_time_val:
            try:
                parts = tiktok_time_val.replace("T", " ").split(" ")
                tiktok_date_val = parts[0]
                tiktok_time_only = parts[1][:5]
            except Exception:
                pass
        self._tiktok_date_entry = ttk.DateEntry(tiktok_sched_frame, bootstyle="info", width=12, dateformat="%Y-%m-%d")
        self._tiktok_date_entry.pack(side=LEFT, padx=5)
        if tiktok_date_val:
            try:
                self._tiktok_date_entry.entry.delete(0, tk.END)
                self._tiktok_date_entry.entry.insert(0, tiktok_date_val)
            except Exception:
                pass
        else:
            try:
                self._tiktok_date_entry.entry.delete(0, tk.END)
            except Exception:
                pass
        self._tiktok_time_var = tk.StringVar(value=tiktok_time_only or "21:00")
        self._tiktok_time_entry = ttk.Entry(tiktok_sched_frame, textvariable=self._tiktok_time_var, width=6, state="normal" if is_editable else "readonly")
        self._tiktok_time_entry.pack(side=LEFT)
        ttk.Label(tiktok_sched_frame, text=" (HH:MM - Local Time)", bootstyle="secondary").pack(side=LEFT, padx=3)

        # ------------------ ACTION BUTTONS FRAME ------------------
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
            elif status == "pending_deployment":
                # Deploy button
                ttk.Button(
                    actions, text="🚀 Deploy / Post Videos",
                    bootstyle="success",
                    command=lambda: self._deploy_project(pid),
                ).pack(side=LEFT, padx=5)
                # Bypass / Direct Done button
                ttk.Button(
                    actions, text="🏁 Mark Done (Bypass Upload)",
                    bootstyle="success-outline",
                    command=lambda: self._mark_project_done_direct(pid),
                ).pack(side=LEFT, padx=5)
                # Reset to Staged button
                ttk.Button(
                    actions, text="📁 Reset to Staged",
                    bootstyle="warning-outline",
                    command=lambda: self._reset_project_to_staged(pid),
                ).pack(side=LEFT, padx=5)
            else:
                ttk.Button(
                    actions, text="✅ Queue for Processing",
                    bootstyle="success",
                    command=lambda: self._save_and_queue(pid),
                ).pack(side=LEFT, padx=5)
                # Mark done button
                ttk.Button(
                    actions, text="🏁 Mark Done",
                    bootstyle="success-outline",
                    command=lambda: self._mark_project_done_direct(pid),
                ).pack(side=LEFT, padx=5)
                if status == "error":
                    ttk.Button(
                        actions, text="📁 Reset to Staged",
                        bootstyle="warning-outline",
                        command=lambda: self._reset_project_to_staged(pid),
                    ).pack(side=LEFT, padx=5)

            ttk.Button(
                actions, text="🗑 Delete Project",
                bootstyle="danger-outline",
                command=lambda: self._delete_project(pid),
            ).pack(side=RIGHT, padx=5)



        self._add_log_viewer(f, pid)
        
        if active_tab_idx is not None:
            try:
                self._detail_notebook.select(active_tab_idx)
            except Exception:
                pass
                
        if scroll_pos is not None:
            try:
                self.after(50, lambda: self._detail_content.yview_moveto(scroll_pos))
            except Exception:
                pass

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

    def _show_canvas_candidates(self, candidates: list[dict],
                                is_editable: bool = True):
        """Display canvas candidates with play buttons."""
        for w in self._canvas_candidates_frame.winfo_children():
            w.destroy()

        ttk.Label(
            self._canvas_candidates_frame,
            text=f"{len(candidates)} canvas candidates found:",
            font=("Helvetica", 10),
            bootstyle="secondary",
        ).pack(anchor=W, pady=(0, 5))

        for i, c in enumerate(candidates):
            row = ttk.Frame(self._canvas_candidates_frame)
            row.pack(fill=X, pady=2)

            num = i + 1
            start_m, start_s = divmod(c["start_time"], 60)
            end_m, end_s = divmod(c["end_time"], 60)
            dur_m, dur_s = divmod(c["duration"], 60)

            text = (
                f"#{num}  "
                f"{int(start_m)}:{start_s:05.2f} → "
                f"{int(end_m)}:{end_s:05.2f}  "
                f"score: {c['score']:.3f}"
            )

            if is_editable:
                ttk.Radiobutton(
                    row, text=text,
                    variable=self._canvas_pick_var, value=num,
                    bootstyle="info",
                ).pack(side=LEFT)
            else:
                ttk.Label(row, text=text).pack(side=LEFT)

            preview_path = c.get("preview_path", "")
            if preview_path and os.path.isfile(preview_path):
                ttk.Button(
                    row, text="▶",
                    bootstyle="success-outline",
                    width=3,
                    command=lambda p=preview_path: self._play_preview(p),
                ).pack(side=RIGHT, padx=5)

    def _show_processing_progress_panel(self, parent, project: dict):
        """Show step-by-step progress for a processing or deploying project."""
        current = project.get("current_step", 0)
        status = project.get("status", "processing")
        total = project.get("total_steps", 13)
        if status == "deploying":
            total = 12
        elif status == "processing":
            total = 13

        # Progress bar
        pct = int(100 * current / max(total, 1))
        self._proc_status_label = ttk.Label(
            parent,
            text=f"Progress: Step {current}/{total}",
            font=("Helvetica", 12),
        )
        self._proc_status_label.pack(anchor=W, pady=5)

        self._proc_progress_bar = ttk.Progressbar(
            parent, maximum=100, value=pct,
            bootstyle="success-striped",
            length=600,
        )
        self._proc_progress_bar.pack(fill=X, pady=5)

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

    def _show_done_summary_panel(self, parent, project: dict):
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

        # Allow re-deploy
        ttk.Button(
            parent, text="🚀 Re-deploy / Publish Video",
            bootstyle="success-outline",
            command=lambda: self._redeploy_project(project["id"]),
        ).pack(anchor=W, pady=5)

    def _show_pending_link_panel(self, parent, project: dict):
        """Show pending link instructions and actions."""
        pid = project["id"]
        name = project["name"]
        
        steps = db.get_steps(self.conn, pid)
        yt_long_id = ""
        yt_short_id = ""
        for step in steps:
            if step["step_num"] == 11 and step["status"] == "done":
                yt_long_id = step["output_file"]
            if step["step_num"] == 12 and step["status"] == "done":
                yt_short_id = step["output_file"]
                
        link_lf = ttk.LabelFrame(parent, text="🔗 YouTube Short Link Pending")
        link_lf.pack(fill=X, pady=(0, 15))
        link_pad = ttk.Frame(link_lf, padding=10)
        link_pad.pack(fill=X)
        
        msg = (
            "Both YouTube Long and YouTube Short have been successfully uploaded!\n\n"
            f"1. Click the 'Open Short in YouTube Studio' button below to go straight to the Short's edit page.\n"
            f"2. Under the 'Related video' setting on the right side, select your main unlisted video:\n"
            f"   » \"{name} 4k\" (ID: {yt_long_id or 'unknown'})\n"
            f"3. Save the Short in YouTube Studio.\n"
            f"4. Click the 'Finish Scheduling Main Video' button to schedule the long-form video."
        )
        ttk.Label(link_pad, text=msg, font=("Helvetica", 10), justify=LEFT, wraplength=650).pack(fill=X)

        actions = ttk.Frame(parent)
        actions.pack(fill=X, pady=10)

        def open_studio():
            if yt_short_id:
                import webbrowser
                webbrowser.open(f"https://studio.youtube.com/video/{yt_short_id}/edit")
            else:
                messagebox.showerror("Error", "YouTube Short ID not found in logs.")

        studio_btn = ttk.Button(
            actions, text="🌐 Open Short in YouTube Studio",
            bootstyle="info",
            command=open_studio,
        )
        studio_btn.pack(side=LEFT, padx=5)

        def copy_long_id():
            if yt_long_id:
                self.clipboard_clear()
                self.clipboard_append(yt_long_id)
                messagebox.showinfo("Copied", f"Copied Main Video ID to clipboard: {yt_long_id}")
            else:
                messagebox.showerror("Error", "YouTube Long Video ID not found.")

        copy_btn = ttk.Button(
            actions, text="📋 Copy Main Video ID",
            bootstyle="info-outline",
            command=copy_long_id,
        )
        copy_btn.pack(side=LEFT, padx=5)

        def finish_schedule():
            def _thread():
                from pipeline.upload import get_youtube_client, update_youtube_video_status, sync_facebook_post_with_youtube_url, sync_youtube_short_with_main_url
                yt_creds, err_creds = get_youtube_client(self.conn)
                if not yt_creds:
                    self.after(0, lambda: messagebox.showerror("Error", f"YouTube credentials not found: {err_creds}"))
                    return
                
                self.after(0, lambda: link_btn.configure(state="disabled", text="Scheduling..."))
                
                success, err = update_youtube_video_status(
                    yt_creds, yt_long_id,
                    project.get("yt_schedule_time", ""),
                    log_callback=print
                )
                if success:
                    db.update_project(self.conn, pid, status="done", error_message="")
                    
                    # Update Facebook post & YouTube Short with the new YouTube URL!
                    try:
                        sync_facebook_post_with_youtube_url(self.conn, pid, log_callback=print)
                    except Exception as sync_err:
                        print(f"Error syncing Facebook post: {sync_err}")

                    try:
                        sync_youtube_short_with_main_url(self.conn, pid, log_callback=print)
                    except Exception as sync_err:
                        print(f"Error syncing YouTube Short: {sync_err}")

                    def done():
                        messagebox.showinfo("Success", "YouTube main video has been scheduled and Facebook post & YouTube Short updated with YouTube URL!")
                        self._select_project(pid)
                    self.after(0, done)
                else:
                    def failed():
                        link_btn.configure(state="normal", text="🔗 Finish Scheduling Main Video")
                        messagebox.showerror("Error", f"Failed to schedule main video: {err}")
                    self.after(0, failed)
            threading.Thread(target=_thread, daemon=True).start()

        link_btn = ttk.Button(
            actions, text="🔗 Finish Scheduling Main Video",
            bootstyle="success",
            command=finish_schedule,
        )
        link_btn.pack(side=LEFT, padx=5)

        ttk.Button(
            actions, text="🔄 Reset / Re-deploy",
            bootstyle="warning-outline",
            command=lambda: self._redeploy_project(pid),
        ).pack(side=LEFT, padx=5)

        ttk.Button(
            actions, text="🗑 Delete Project",
            bootstyle="danger-outline",
            command=lambda: self._delete_project(pid),
        ).pack(side=RIGHT, padx=5)

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
            if step["status"] in ("done", "running", "error") or (step.get("log_text") and step.get("log_text").strip()):
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

    def _get_schedule_time(self, date_entry, time_var) -> str:
        try:
            date_str = date_entry.entry.get().strip()
        except Exception:
            return ""
        if not date_str:
            return ""
        
        time_str = time_var.get().strip()
        if not time_str:
            time_str = "00:00"
            
        try:
            # support date format like YYYY-MM-DD
            date_str = date_str.split()[0]
            if ":" not in time_str:
                time_str += ":00"
            # parse
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            return dt.isoformat()
        except Exception:
            return ""

    def _save_project(self, pid: int):
        """Save current form values to the database."""
        text = self._text_widget.get("1.0", "end-1c").strip()
        
        # Read the publishing and scheduling fields
        description_body = self._description_body_text.get("1.0", "end-1c").strip()
        fb_post_body = self._fb_post_body_text.get("1.0", "end-1c").strip()
        yt_title_body = self._yt_title_body_var.get().strip()
        yt_desc_body = self._yt_desc_body_text.get("1.0", "end-1c").strip()
        short_title_body = self._short_title_body_var.get().strip()
        short_desc_body = self._short_desc_body_text.get("1.0", "end-1c").strip()
        tiktok_title_body = self._tiktok_title_body_var.get().strip() if hasattr(self, "_tiktok_title_body_var") else ""
        tiktok_desc_body = self._tiktok_desc_body_text.get("1.0", "end-1c").strip() if hasattr(self, "_tiktok_desc_body_text") else ""
        
        fb_schedule_time = self._get_schedule_time(self._fb_date_entry, self._fb_time_var)
        yt_schedule_time = self._get_schedule_time(self._yt_date_entry, self._yt_time_var)
        short_schedule_time = self._get_schedule_time(self._short_date_entry, self._short_time_var)
        tiktok_schedule_time = self._get_schedule_time(self._tiktok_date_entry, self._tiktok_time_var) if hasattr(self, "_tiktok_date_entry") else ""

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
            process_tiktok=int(self._proc_tiktok_var.get()) if hasattr(self, "_proc_tiktok_var") else 0,
            negative_logo=int(self._neg_logo_var.get()),
            short_source_fb=int(self._short_src_fb_var.get()),
            generate_spotify_canvas=int(self._proc_canvas_var.get()),
            overlay_text=text,
            font_size=self._fontsize_var.get(),
            fade_in_start=self._fi_start_var.get(),
            fade_in_end=self._fi_end_var.get(),
            fade_out_start=self._fo_start_var.get(),
            fade_out_end=self._fo_end_var.get(),
            loop_pick=self._loop_pick_var.get(),
            loop_fade=self._loop_fade_var.get(),
            spotify_canvas_pick=self._canvas_pick_var.get(),
            loop_min_dur=self._loop_min_dur_var.get(),
            loop_max_dur=self._loop_max_dur_var.get(),
            description_body=description_body,
            fb_post_body=fb_post_body,
            yt_title_body=yt_title_body,
            yt_description_body=yt_desc_body,
            short_title_body=short_title_body,
            short_description_body=short_desc_body,
            tiktok_title_body=tiktok_title_body,
            tiktok_description_body=tiktok_desc_body,
            fb_schedule_time=fb_schedule_time,
            yt_schedule_time=yt_schedule_time,
            short_schedule_time=short_schedule_time,
            tiktok_schedule_time=tiktok_schedule_time,
            tags=self._tags_entry_var.get().strip() if hasattr(self, "_tags_entry_var") else "",
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
        proc_tiktok = self._proc_tiktok_var.get() if hasattr(self, "_proc_tiktok_var") else False
        short_src_fb = self._short_src_fb_var.get()

        proc_canvas = self._proc_canvas_var.get()

        errors = []
        if not proc_fb and not proc_yt and not proc_shorts and not proc_canvas and not proc_tiktok:
            errors.append("At least one target (Facebook, YouTube, YouTube Short, TikTok Short, or Spotify Canvas) must be enabled for processing")

        if proc_yt or proc_shorts or proc_canvas:
            needs_yt_video = proc_canvas or (not short_src_fb and (proc_yt or proc_shorts))
            if needs_yt_video:
                if not yt_video or not os.path.isfile(yt_video):
                    errors.append("YouTube Video (Long) file is missing or not found")
            if proc_yt or proc_shorts:
                if not audio or not os.path.isfile(audio):
                    errors.append("YouTube Audio (Long) file is missing or not found")
            if proc_shorts and not text:
                errors.append("YouTube short text is empty")
            if proc_shorts and short_src_fb and (not fb_video or not os.path.isfile(fb_video)):
                errors.append("'Use FB Video for Short' is enabled but Facebook Video (Short) file is missing or not found")

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

    def _reset_project_to_staged(self, pid: int):
        if not messagebox.askyesno(
            "Reset to Staged",
            "Are you sure you want to move this project back to Staged?\n"
            "This will clear the current process logs and set status to Staged.",
        ):
            return
        db.update_project(
            self.conn, pid,
            status="staged",
            current_step=0,
            queued_at=None,
            error_message="",
        )
        self.conn.execute("DELETE FROM process_log WHERE project_id = ?", (pid,))
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _generate_gemini_tags(self, pid: int):
        api_key = db.get_setting(self.conn, "gemini_api_key", "").strip()
        if not api_key:
            messagebox.showwarning("Missing API Key", "Please configure your Gemini API Key in Settings first.")
            return

        project = db.get_project(self.conn, pid)
        if not project:
            return

        desc_body = self._description_body_text.get("1.0", "end-1c").strip()
        
        if hasattr(self, "_gemini_tags_btn") and self._gemini_tags_btn:
            self._gemini_tags_btn.configure(state="disabled", text="⚡ Generating...")

        def thread_func():
            try:
                import requests
                import base64
                import mimetypes
                
                audio_path = project.get("audio_file")
                audio_part = None
                
                if audio_path and os.path.isfile(audio_path):
                    file_size = os.path.getsize(audio_path)
                    if file_size <= 8 * 1024 * 1024:  # 8 MB safety limit to avoid HTTP 413 Payload Too Large
                        try:
                            mime_type, _ = mimetypes.guess_type(audio_path)
                            if not mime_type:
                                mime_type = "audio/mpeg" if audio_path.lower().endswith(".mp3") else "audio/wav"
                            
                            with open(audio_path, "rb") as af:
                                audio_bytes = af.read()
                                audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
                                
                            audio_part = {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": audio_b64
                                }
                            }
                        except Exception:
                            pass
                
                gemini_model = db.get_setting(self.conn, "gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
                url = f"https://generativelanguage.googleapis.com/v1/models/{gemini_model}:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}
                
                parts = []
                if audio_part:
                    parts.append(audio_part)
                    prompt = f"""
We are publishing this relaxing music video on YouTube and Facebook.
Please analyze the attached music/audio file and generate 10-15 high-quality tags/keywords for this video based on the music itself (genre, mood, instruments, rhythm, etc.) and the following metadata:
- Title/Project Name: {project['name']}
- Quote/Description: {desc_body}

Generate ONLY a comma-separated list of clean tags (no hashtags, just lowercase tags. E.g. relaxing music, sleep music, ambient, etc.). Do not include any introductory text or explanation, just the tags themselves.
"""
                else:
                    prompt = f"""
We are publishing a relaxing music video on YouTube and Facebook.
Please generate 10-15 high-quality tags/keywords for this video based on the following metadata:
- Title/Project Name: {project['name']}
- Music File: {os.path.basename(project['audio_file'] or '')}
- Quote/Description: {desc_body}
- Folder Path: {project['folder_path']}

Generate ONLY a comma-separated list of clean tags (no hashtags, just lowercase tags. E.g. relaxing music, sleep music, ambient, etc.). Do not include any introductory text or explanation, just the tags themselves.
"""
                parts.append({"text": prompt})
                
                payload = {
                    "contents": [{
                        "parts": parts
                    }]
                }
                
                import time
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        res = requests.post(url, headers=headers, json=payload, timeout=180)
                        if res.status_code == 503 and attempt < max_retries - 1:
                            time.sleep(2 * (attempt + 1))
                            continue
                        res.raise_for_status()
                        break
                    except requests.exceptions.RequestException as e:
                        if attempt == max_retries - 1:
                            raise e
                        time.sleep(2 * (attempt + 1))
                
                result = res.json()
                if "candidates" not in result or not result["candidates"]:
                    raise RuntimeError("No candidates returned. Response might be blocked by safety filters or empty.")
                
                candidate = result["candidates"][0]
                if "content" not in candidate or "parts" not in candidate["content"] or not candidate["content"]["parts"]:
                    finish_reason = candidate.get("finishReason", "UNKNOWN")
                    raise RuntimeError(f"No content returned in candidate (Finish Reason: {finish_reason}).")
                
                generated_tags = candidate["content"]["parts"][0]["text"].strip()
                
                # Strip leading/trailing brackets, quotes or extra symbols if any
                generated_tags = generated_tags.replace("[", "").replace("]", "").replace("`", "")
                
                def success(tags):
                    current_tags = self._tags_entry_var.get().strip()
                    base_tags = current_tags if current_tags else db.get_setting(self.conn, "default_tags", "")
                    
                    existing_list = []
                    seen_lower = set()
                    for t in base_tags.split(","):
                        cleaned_t = t.strip()
                        if cleaned_t:
                            lower_t = cleaned_t.lower()
                            if lower_t not in seen_lower:
                                existing_list.append(cleaned_t)
                                seen_lower.add(lower_t)
                                
                    new_list = []
                    for t in tags.split(","):
                        cleaned_t = t.strip()
                        if cleaned_t:
                            lower_t = cleaned_t.lower()
                            if lower_t not in seen_lower:
                                new_list.append(cleaned_t)
                                seen_lower.add(lower_t)
                                
                    combined_list = existing_list + new_list
                    cleaned = ", ".join(combined_list)
                    self._tags_entry_var.set(cleaned)
                    self._save_project(pid)
                    messagebox.showinfo("Success", "Gemini successfully generated and added tags!")

                self.after(0, lambda: success(generated_tags))
            except Exception as e:
                err_msg = str(e)
                if hasattr(e, "response") and e.response is not None:
                    try:
                        err_msg += f"\n\nServer Response: {e.response.text}"
                    except Exception:
                        pass
                def failed(err):
                    messagebox.showerror("Error", f"Failed to generate tags with Gemini: {err}")
                self.after(0, lambda err_str=err_msg: failed(err_str))
            finally:
                def reset_btn():
                    if hasattr(self, "_gemini_tags_btn") and self._gemini_tags_btn:
                        self._gemini_tags_btn.configure(state="normal", text="✨ Gemini Tags")
                self.after(0, reset_btn)

        threading.Thread(target=thread_func, daemon=True).start()

    def _generate_gemini_quote(self, pid: int):
        api_key = db.get_setting(self.conn, "gemini_api_key", "").strip()
        if not api_key:
            messagebox.showwarning("Missing API Key", "Please configure your Gemini API Key in Settings first.")
            return

        project = db.get_project(self.conn, pid)
        if not project:
            return

        rule_template = db.get_setting(
            self.conn, "gemini_quote_template",
            'Generate a two-sentence quote for this sleep song.  Keep it simple. Generate the quote for the song titled "{{title}}".'
        )
        rule_text = rule_template.replace("{{title}}", project["name"])

        if hasattr(self, "_gemini_quote_btn") and self._gemini_quote_btn:
            self._gemini_quote_btn.configure(state="disabled", text="⚡ Generating...")

        def thread_func():
            try:
                import requests
                gemini_model = db.get_setting(self.conn, "gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
                url = f"https://generativelanguage.googleapis.com/v1/models/{gemini_model}:generateContent?key={api_key}"
                headers = {"Content-Type": "application/json"}
                prompt = f"""
{rule_text}

Additionally, select the best, most poetic part of that generated quote to be used as a short text overlay (max 100 characters, no hashtags).

Please format your response as a raw JSON object with exactly two keys:
"quote": "the full generated two-sentence quote"
"short_text": "the best, most poetic part of the quote"

Do not include any other text, markdown formatting (like ```json), or explanations. Return only the raw JSON.
"""
                payload = {
                    "contents": [{
                        "parts": [{"text": prompt}]
                    }]
                }
                import time
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        res = requests.post(url, headers=headers, json=payload, timeout=60)
                        if res.status_code == 503 and attempt < max_retries - 1:
                            time.sleep(2 * (attempt + 1))
                            continue
                        res.raise_for_status()
                        break
                    except requests.exceptions.RequestException as e:
                        if attempt == max_retries - 1:
                            raise e
                        time.sleep(2 * (attempt + 1))
                
                result = res.json()
                if "candidates" not in result or not result["candidates"]:
                    raise RuntimeError("No candidates returned. Response might be blocked by safety filters or empty.")
                
                candidate = result["candidates"][0]
                if "content" not in candidate or "parts" not in candidate["content"] or not candidate["content"]["parts"]:
                    finish_reason = candidate.get("finishReason", "UNKNOWN")
                    raise RuntimeError(f"No content returned in candidate (Finish Reason: {finish_reason}).")
                
                text_response = candidate["content"]["parts"][0]["text"].strip()
                
                # Strip markdown json block wrappers if Gemini ignored the instruction
                if text_response.startswith("```"):
                    lines = text_response.splitlines()
                    if len(lines) >= 3:
                        text_response = "\n".join(lines[1:-1]).strip()
                        if text_response.startswith("json"):
                            text_response = text_response[4:].strip()
                
                # Parse JSON
                data = json.loads(text_response)
                quote_val = data.get("quote", "").strip()
                short_text_val = data.get("short_text", "").strip()

                def success(q, s):
                    # Update description body in UI
                    self._description_body_text.delete("1.0", tk.END)
                    self._description_body_text.insert("1.0", q)
                    
                    # Update Short Text (overlay text) in Tab 2
                    self._text_widget.configure(state="normal")
                    self._text_widget.delete("1.0", tk.END)
                    self._text_widget.insert("1.0", s)
                    if self.selected_project_id != pid or self._current_detail_project_status not in ("staged", "error", "queued", "pending_deployment", "done"):
                        self._text_widget.configure(state="disabled")
                    
                    # Run template updates to re-render facebook/youtube posts
                    if hasattr(self, "_re_render_all_templates_callback") and self._re_render_all_templates_callback:
                        self._re_render_all_templates_callback(show_info=False)
                    
                    self._save_project(pid)
                    messagebox.showinfo("Success", "Gemini successfully generated the quote and short text!")

                self.after(0, lambda: success(quote_val, short_text_val))
            except Exception as e:
                err_msg = str(e)
                if hasattr(e, "response") and e.response is not None:
                    try:
                        err_msg += f"\n\nServer Response: {e.response.text}"
                    except Exception:
                        pass
                def failed(err):
                    messagebox.showerror("Error", f"Failed to generate quote with Gemini: {err}")
                self.after(0, lambda err_str=err_msg: failed(err_str))
            finally:
                def reset_btn():
                    if hasattr(self, "_gemini_quote_btn") and self._gemini_quote_btn:
                        self._gemini_quote_btn.configure(state="normal", text="✨ Gemini Quote")
                self.after(0, reset_btn)

        threading.Thread(target=thread_func, daemon=True).start()

    def _mark_project_done_direct(self, pid: int):
        if not messagebox.askyesno(
            "Mark Done",
            "Mark this project completed directly?\n"
            "This will bypass all background processing and upload steps.",
        ):
            return
        db.update_project(
            self.conn, pid,
            status="done",
            current_step=14,
            fb_upload_status="done",
            yt_upload_status="done",
            short_upload_status="done",
            tiktok_upload_status="done",
            error_message="",
        )
        # Seed fake/noop steps in the process log so it displays correctly
        self.conn.execute("DELETE FROM process_log WHERE project_id = ?", (pid,))
        for step_num in range(1, 15):
            self.conn.execute(
                """INSERT INTO process_log 
                   (project_id, step_num, step_name, status, output_file, log_text)
                   VALUES (?, ?, ?, 'done', 'Bypassed', 'Manually marked as completed')""",
                (pid, step_num, STEP_NAMES[step_num]),
            )
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _redeploy_project(self, pid: int):
        db.update_project(
            self.conn, pid,
            status="pending_deployment",
            fb_upload_status="pending",
            yt_upload_status="pending",
            short_upload_status="pending",
            tiktok_upload_status="pending",
            error_message="",
        )
        self.conn.execute(
            "UPDATE process_log SET status = 'pending', started_at = NULL, finished_at = NULL, output_file = '', log_text = '' "
            "WHERE project_id = ? AND step_num IN (10, 11, 12, 14)",
            (pid,)
        )
        self.conn.commit()
        self._refresh_list()
        self._select_project(pid)

    def _deploy_project(self, pid: int):
        """Save form data, validate credentials, and start background deployment."""
        self._save_project(pid)
        
        project = db.get_project(self.conn, pid)
        if not project:
            return
            
        deploy_fb = self._deploy_fb_var.get() if hasattr(self, "_deploy_fb_var") else True
        deploy_yt = self._deploy_yt_var.get() if hasattr(self, "_deploy_yt_var") else True
        deploy_short = self._deploy_short_var.get() if hasattr(self, "_deploy_short_var") else True
        deploy_tiktok = self._deploy_tiktok_var.get() if hasattr(self, "_deploy_tiktok_var") else True

        # Check if at least one selected platform is going to be deployed
        any_selected = deploy_fb or deploy_yt or deploy_short or deploy_tiktok
        if not any_selected:
            messagebox.showwarning(
                "No Platforms Selected",
                "Please check at least one platform to deploy (Facebook, YouTube Long, YouTube Short, or TikTok Short)."
            )
            return

        errors = []
        if deploy_fb:
            fb_page_id = db.get_setting(self.conn, "fb_page_id", "").strip()
            fb_token = db.get_setting(self.conn, "fb_page_token", "").strip()
            if not fb_page_id or not fb_token:
                errors.append("Facebook Page ID and Page Access Token are missing in Settings.")
                
        if deploy_yt or deploy_short:
            secrets_json = db.get_setting(self.conn, "yt_client_secrets", "").strip()
            if not secrets_json:
                errors.append("YouTube client secrets JSON is missing in Settings.")
            else:
                from pipeline.upload import get_youtube_client
                creds, err = get_youtube_client(self.conn)
                if not creds:
                    errors.append(f"YouTube credentials: {err}")

        if deploy_tiktok:
            from pipeline.upload import get_tiktok_client
            tt_creds, err_tt = get_tiktok_client(self.conn)
            if not tt_creds:
                errors.append(f"TikTok credentials: {err_tt}")
                
        if errors:
            messagebox.showwarning(
                "Configuration Required",
                "Please configure settings for enabled platforms:\n\n• " + "\n• ".join(errors)
            )
            return

        db.update_project(self.conn, pid, status="deploying", current_step=9)
        self._refresh_list()
        self._select_project(pid)
        
        thread = threading.Thread(
            target=self._run_deployment_thread,
            args=(pid, deploy_fb, deploy_yt, deploy_short, deploy_tiktok),
            daemon=True
        )
        thread.start()

    def _run_deployment_thread(self, pid: int, deploy_fb: bool, deploy_yt: bool, deploy_short: bool, deploy_tiktok: bool = True):
        conn = db.get_connection(DB_PATH)
        
        def notify(step_num, status, msg):
            self.after(0, lambda: self._on_pipeline_update(pid, step_num, status, msg))
            
        try:
            project = db.get_project(conn, pid)
            if not project:
                return
                
            folder = project["folder_path"]
            name = project["name"]
            tags_str = project.get("tags", "")
            tags_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
            
            fb_video_path = os.path.join(folder, f"{name} 4k - Facebook.mp4")
            yt_video_path = os.path.join(folder, f"{name} 4k (Video).mp4")
            short_video_path = os.path.join(folder, f"{name} - YouTube Short.mp4")
            
            # Ensure steps 10-12, 14 exist in process_log
            existing_steps = {s["step_num"]: s for s in db.get_steps(conn, pid)}
            for step_num in (10, 11, 12, 14):
                if step_num not in existing_steps:
                    db.log_step(conn, pid, step_num, STEP_NAMES[step_num], "pending")
                    
            from pipeline.upload import (
                upload_to_facebook_page,
                upload_to_youtube,
                upload_to_tiktok,
                get_youtube_client,
                get_tiktok_client,
                render_template,
                sync_facebook_post_with_youtube_url
            )
            
            errors = []
            
            # ----------------- FB UPLOAD (Step 10) -----------------
            if deploy_fb:
                if project.get("fb_upload_status") != "done":
                    if not os.path.isfile(fb_video_path):
                        err_msg = f"Facebook video file not found: {fb_video_path}"
                        errors.append(err_msg)
                        db.update_step(conn, pid, 10, status="error", log_text=err_msg)
                        db.update_project(conn, pid, fb_upload_status="error")
                    else:
                        db.update_step(conn, pid, 10, status="running", started_at=datetime.now().isoformat())
                        db.update_project(conn, pid, current_step=10)
                        notify(10, "running", "Uploading Facebook Video...")
                        
                        fb_page_id = db.get_setting(conn, "fb_page_id", "").strip()
                        fb_token = db.get_setting(conn, "fb_page_token", "").strip()
                        if not fb_token:
                            fb_token = db.get_setting(conn, "fb_long_user_token", "").strip()
                        fb_desc = render_template(project.get("fb_post_body", ""), project.get("overlay_text", ""), name)
                        
                        fb_log_lines = []
                        def log_cb_fb(txt):
                            fb_log_lines.append(txt)
                            db.update_step(conn, pid, 10, log_text="\n".join(fb_log_lines))
                            notify(10, "running", txt)
                            
                        vid_id, err = upload_to_facebook_page(
                            fb_page_id, fb_token, fb_video_path,
                            name, fb_desc, project.get("fb_schedule_time", ""),
                            tags=tags_list,
                            log_callback=log_cb_fb
                        )
                        
                        if err:
                            errors.append(f"Facebook upload: {err}")
                            db.update_step(conn, pid, 10, status="error", finished_at=datetime.now().isoformat(), log_text=f"Error: {err}")
                            db.update_project(conn, pid, fb_upload_status="error")
                        else:
                            db.update_step(conn, pid, 10, status="done", finished_at=datetime.now().isoformat(), output_file=vid_id, log_text=f"Uploaded successfully. Video ID: {vid_id}")
                            db.update_project(conn, pid, fb_upload_status="done")
                            notify(10, "done", "Facebook Video Uploaded")
                            
                            # Sync Facebook post with YouTube URL if YouTube video is already uploaded
                            try:
                                ok_sync, msg_sync = sync_facebook_post_with_youtube_url(conn, pid, log_callback=log_cb_fb)
                                if ok_sync:
                                    fb_log_lines.append(f"Facebook Post Sync: {msg_sync}")
                                    db.update_step(conn, pid, 10, log_text="\n".join(fb_log_lines))
                            except Exception:
                                pass
                else:
                    db.update_step(conn, pid, 10, status="done", log_text="Bypassed (Facebook already uploaded)")
            else:
                if project.get("fb_upload_status") == "done":
                    db.update_step(conn, pid, 10, status="done", log_text="Facebook marked as completed")
                else:
                    db.update_step(conn, pid, 10, status="pending", log_text="Skipped in this deployment run (unchecked)")
                
            # ----------------- YT LONG UPLOAD (Step 11) -----------------
            yt_creds = None
            if deploy_yt:
                if project.get("yt_upload_status") != "done":
                    if not os.path.isfile(yt_video_path):
                        err_msg = f"YouTube long video file not found: {yt_video_path}"
                        errors.append(err_msg)
                        db.update_step(conn, pid, 11, status="error", log_text=err_msg)
                        db.update_project(conn, pid, yt_upload_status="error")
                    else:
                        yt_creds, err_creds = get_youtube_client(conn)
                        if not yt_creds:
                            errors.append(f"YouTube credentials: {err_creds}")
                            db.update_step(conn, pid, 11, status="error", log_text=err_creds)
                            db.update_project(conn, pid, yt_upload_status="error")
                        else:
                            db.update_step(conn, pid, 11, status="running", started_at=datetime.now().isoformat())
                            db.update_project(conn, pid, current_step=11)
                            notify(11, "running", "Uploading YouTube Video...")
                            
                            yt_title = render_template(project.get("yt_title_body", ""), project.get("overlay_text", ""), name)
                            yt_desc = render_template(project.get("yt_description_body", ""), project.get("overlay_text", ""), name)
                            
                            is_both_yt = (
                                (project.get("process_youtube", 1) and not project.get("skip_shorts", 0)) or
                                (os.path.isfile(yt_video_path) and os.path.isfile(short_video_path))
                            )
                            
                            yt_log_lines = []
                            def log_cb_yt(txt):
                                yt_log_lines.append(txt)
                                db.update_step(conn, pid, 11, log_text="\n".join(yt_log_lines))
                                notify(11, "running", txt)
                                
                            vid_id, err = upload_to_youtube(
                                yt_creds, yt_video_path,
                                yt_title, yt_desc,
                                schedule_time_iso="" if is_both_yt else project.get("yt_schedule_time", ""),
                                privacy_status="unlisted" if is_both_yt else "public",
                                tags=tags_list,
                                log_callback=log_cb_yt
                            )
                            
                            if err:
                                errors.append(f"YouTube Long upload: {err}")
                                db.update_step(conn, pid, 11, status="error", finished_at=datetime.now().isoformat(), log_text=f"Error: {err}")
                                db.update_project(conn, pid, yt_upload_status="error")
                            else:
                                db.update_step(conn, pid, 11, status="done", finished_at=datetime.now().isoformat(), output_file=vid_id, log_text=f"Uploaded successfully. Video ID: {vid_id}")
                                db.update_project(conn, pid, yt_upload_status="done")
                                notify(11, "done", "YouTube Video Uploaded")
                                
                                # Automatically update Facebook post & YouTube Short descriptions with the new YouTube URL
                                try:
                                    ok_sync, msg_sync = sync_facebook_post_with_youtube_url(conn, pid, log_callback=log_cb_yt)
                                    if ok_sync:
                                        yt_log_lines.append(f"Facebook Post Sync: {msg_sync}")
                                        db.update_step(conn, pid, 11, log_text="\n".join(yt_log_lines))
                                except Exception:
                                    pass

                                try:
                                    from pipeline.upload import sync_youtube_short_with_main_url
                                    ok_sync_s, msg_sync_s = sync_youtube_short_with_main_url(conn, pid, log_callback=log_cb_yt)
                                    if ok_sync_s:
                                        yt_log_lines.append(f"YouTube Short Sync: {msg_sync_s}")
                                        db.update_step(conn, pid, 11, log_text="\n".join(yt_log_lines))
                                except Exception:
                                    pass
                else:
                    db.update_step(conn, pid, 11, status="done", log_text="Bypassed (YouTube Long already uploaded)")
            else:
                if project.get("yt_upload_status") == "done":
                    db.update_step(conn, pid, 11, status="done", log_text="YouTube Long marked as completed")
                else:
                    db.update_step(conn, pid, 11, status="pending", log_text="Skipped in this deployment run (unchecked)")
                
            # ----------------- YT SHORT UPLOAD (Step 12) -----------------
            if deploy_short:
                if project.get("short_upload_status") != "done":
                    if not os.path.isfile(short_video_path):
                        err_msg = f"YouTube Short video file not found: {short_video_path}"
                        errors.append(err_msg)
                        db.update_step(conn, pid, 12, status="error", log_text=err_msg)
                        db.update_project(conn, pid, short_upload_status="error")
                    else:
                        if not yt_creds:
                            yt_creds, err_creds = get_youtube_client(conn)
                        if not yt_creds:
                            errors.append(f"YouTube credentials (Short): {err_creds}")
                            db.update_step(conn, pid, 12, status="error", log_text=err_creds)
                            db.update_project(conn, pid, short_upload_status="error")
                        else:
                            db.update_step(conn, pid, 12, status="running", started_at=datetime.now().isoformat())
                            db.update_project(conn, pid, current_step=12)
                            notify(12, "running", "Uploading YouTube Short...")
                            
                            main_yt_id = ""
                            for s in db.get_steps(conn, pid):
                                if s["step_num"] == 11 and s.get("output_file") and not s["output_file"].startswith("Bypassed"):
                                    main_yt_id = s["output_file"]
                                    break
                            short_yt_url = f"https://www.youtube.com/watch?v={main_yt_id}" if main_yt_id else ""
                            short_overlay = project.get("overlay_text", "")

                            short_title = render_template(project.get("short_title_body", ""), project.get("overlay_text", ""), name, yt_url=short_yt_url, overlay_text=short_overlay)
                            short_desc = render_template(project.get("short_description_body", ""), project.get("overlay_text", ""), name, yt_url=short_yt_url, overlay_text=short_overlay)
                            
                            short_log_lines = []
                            def log_cb_short(txt):
                                short_log_lines.append(txt)
                                db.update_step(conn, pid, 12, log_text="\n".join(short_log_lines))
                                notify(12, "running", txt)
                                
                            vid_id, err = upload_to_youtube(
                                yt_creds, short_video_path,
                                short_title, short_desc, project.get("short_schedule_time", ""),
                                tags=tags_list,
                                log_callback=log_cb_short
                            )
                            
                            if err:
                                errors.append(f"YouTube Short upload: {err}")
                                db.update_step(conn, pid, 12, status="error", finished_at=datetime.now().isoformat(), log_text=f"Error: {err}")
                                db.update_project(conn, pid, short_upload_status="error")
                            else:
                                db.update_step(conn, pid, 12, status="done", finished_at=datetime.now().isoformat(), output_file=vid_id, log_text=f"Uploaded successfully. Video ID: {vid_id}")
                                db.update_project(conn, pid, short_upload_status="done")
                                notify(12, "done", "YouTube Short Uploaded")

                                # Automatically post comment on Short linking to main video if main video exists
                                try:
                                    from pipeline.upload import post_youtube_short_comment
                                    ok_c, msg_c = post_youtube_short_comment(conn, pid, log_callback=log_cb_short)
                                    if ok_c:
                                        short_log_lines.append(f"Comment: {msg_c}")
                                        db.update_step(conn, pid, 12, log_text="\n".join(short_log_lines))
                                except Exception:
                                    pass
                else:
                    db.update_step(conn, pid, 12, status="done", log_text="Bypassed (YouTube Short already uploaded)")
            else:
                if project.get("short_upload_status") == "done":
                    db.update_step(conn, pid, 12, status="done", log_text="YouTube Short marked as completed")
                else:
                    db.update_step(conn, pid, 12, status="pending", log_text="Skipped in this deployment run (unchecked)")

            # ----------------- TIKTOK SHORT UPLOAD (Step 14) -----------------
            if deploy_tiktok:
                if project.get("tiktok_upload_status") != "done":
                    if not os.path.isfile(short_video_path):
                        err_msg = f"Short video file not found for TikTok: {short_video_path}"
                        errors.append(err_msg)
                        db.update_step(conn, pid, 14, status="error", log_text=err_msg)
                        db.update_project(conn, pid, tiktok_upload_status="error")
                    else:
                        tt_creds, err_tt = get_tiktok_client(conn)
                        if not tt_creds:
                            errors.append(f"TikTok credentials: {err_tt}")
                            db.update_step(conn, pid, 14, status="error", log_text=err_tt)
                            db.update_project(conn, pid, tiktok_upload_status="error")
                        else:
                            db.update_step(conn, pid, 14, status="running", started_at=datetime.now().isoformat())
                            db.update_project(conn, pid, current_step=14)
                            notify(14, "running", "Uploading TikTok Short...")
                            
                            main_yt_id = ""
                            for s in db.get_steps(conn, pid):
                                if s["step_num"] == 11 and s.get("output_file") and not s["output_file"].startswith("Bypassed"):
                                    main_yt_id = s["output_file"]
                                    break
                            tt_yt_url = f"https://www.youtube.com/watch?v={main_yt_id}" if main_yt_id else ""
                            tt_overlay = project.get("overlay_text", "")

                            tt_title = render_template(project.get("tiktok_title_body", ""), project.get("overlay_text", ""), name, yt_url=tt_yt_url, overlay_text=tt_overlay)
                            tt_desc = render_template(project.get("tiktok_description_body", ""), project.get("overlay_text", ""), name, yt_url=tt_yt_url, overlay_text=tt_overlay)
                            
                            tt_log_lines = []
                            def log_cb_tt(txt):
                                tt_log_lines.append(txt)
                                db.update_step(conn, pid, 14, log_text="\n".join(tt_log_lines))
                                notify(14, "running", txt)
                                
                            tt_privacy = db.get_setting(conn, "tiktok_privacy_level", "SELF_ONLY").strip() or "SELF_ONLY"
                            pub_id, err = upload_to_tiktok(
                                tt_creds.get("access_token", ""),
                                short_video_path,
                                title=tt_title,
                                description=tt_desc,
                                schedule_time_iso=project.get("tiktok_schedule_time", ""),
                                privacy_level=tt_privacy,
                                tags=tags_list,
                                log_callback=log_cb_tt,
                                conn=conn
                            )
                            
                            if err:
                                errors.append(f"TikTok upload: {err}")
                                db.update_step(conn, pid, 14, status="error", finished_at=datetime.now().isoformat(), log_text=f"Error: {err}")
                                db.update_project(conn, pid, tiktok_upload_status="error")
                            else:
                                db.update_step(conn, pid, 14, status="done", finished_at=datetime.now().isoformat(), output_file=pub_id, log_text=f"Uploaded successfully to TikTok. Publish ID: {pub_id}")
                                db.update_project(conn, pid, tiktok_upload_status="done")
                                notify(14, "done", "TikTok Short Uploaded")
                else:
                    db.update_step(conn, pid, 14, status="done", log_text="Bypassed (TikTok already uploaded)")
            else:
                if project.get("tiktok_upload_status") == "done":
                    db.update_step(conn, pid, 14, status="done", log_text="TikTok marked as completed")
                else:
                    db.update_step(conn, pid, 14, status="pending", log_text="Skipped in this deployment run (unchecked)")
                
            # Finalize
            if errors:
                err_msg = "; ".join(errors)
                db.update_project(conn, pid, status="pending_deployment", error_message=err_msg)
                notify(12, "error", f"Deployment failed: {err_msg}")
            else:
                # Query the latest project status
                project = db.get_project(conn, pid)
                
                need_fb = (
                    bool(project.get("process_facebook") if project.get("process_facebook") is not None else 1) or
                    os.path.isfile(fb_video_path) or
                    project.get("fb_upload_status") == "done"
                )
                need_yt = (
                    bool(project.get("process_youtube") if project.get("process_youtube") is not None else 1) or
                    os.path.isfile(yt_video_path) or
                    project.get("yt_upload_status") == "done"
                )
                need_short = (
                    (bool(project.get("process_youtube") if project.get("process_youtube") is not None else 1) and not bool(project.get("skip_shorts") if project.get("skip_shorts") is not None else 0)) or
                    os.path.isfile(short_video_path) or
                    project.get("short_upload_status") == "done"
                )
                need_tiktok = (
                    bool(project.get("process_tiktok") if project.get("process_tiktok") is not None else 0) or
                    project.get("tiktok_upload_status") == "done"
                )
                
                fb_ok = not need_fb or (project.get("fb_upload_status") == "done")
                yt_ok = not need_yt or (project.get("yt_upload_status") == "done")
                short_ok = not need_short or (project.get("short_upload_status") == "done")
                tiktok_ok = not need_tiktok or (project.get("tiktok_upload_status") == "done")
                
                all_done = fb_ok and yt_ok and short_ok and tiktok_ok
                
                if not all_done:
                    db.update_project(conn, pid, status="pending_deployment", error_message="")
                    notify(12, "done", "Selected deployments completed. Other platforms are still pending.")
                else:
                    is_both_yt = (
                        (project.get("process_youtube", 1) and not project.get("skip_shorts", 0)) or
                        (os.path.isfile(yt_video_path) and os.path.isfile(short_video_path))
                    )
                    if is_both_yt:
                        db.update_project(conn, pid, status="pending_link", error_message="", current_step=12)
                        notify(12, "done", "Videos uploaded. Pending related video link.")
                    else:
                        db.update_project(conn, pid, status="done", error_message="", current_step=12)
                        notify(12, "done", "Deployment completed successfully!")
                
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            db.update_project(conn, pid, status="pending_deployment", error_message=str(e))
            notify(12, "error", f"Deployment crashed: {e}\n{tb}")
        finally:
            conn.close()

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

        min_dur = self._loop_min_dur_var.get()
        max_dur = self._loop_max_dur_var.get()
        max_dur_val = max_dur if max_dur > 0.0 else None

        def _work():
            try:
                proxy_path, candidates = generate_all_previews(
                    video, audio, project["folder_path"],
                    n_candidates=5,
                    force=True,
                    min_duration=min_dur,
                    max_duration=max_dur_val,
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
        if hasattr(self, "_analyze_canvas_btn"):
            self._analyze_canvas_btn.configure(text=msg)

    def _reset_analyze_btn(self):
        if hasattr(self, "_analyze_btn"):
            self._analyze_btn.configure(
                state="normal", text="▶ Analyze Loops",
            )
        if hasattr(self, "_analyze_canvas_btn"):
            self._analyze_canvas_btn.configure(
                state="normal", text="▶ Analyze Canvas",
            )

    def _analyze_canvas(self, pid: int):
        video = self._yt_video_var.get()
        audio = self._audio_var.get()

        if not video or not os.path.isfile(video):
            messagebox.showwarning("Missing", "Please select a YouTube Video (Long) first.")
            return

        project = db.get_project(self.conn, pid)
        if not project:
            return

        self._save_project(pid)

        if hasattr(self, "_analyze_canvas_btn"):
            self._analyze_canvas_btn.configure(
                state="disabled", text="Analyzing…",
            )

        def _work():
            try:
                from pipeline.preview import generate_canvas_previews
                proxy_path, candidates = generate_canvas_previews(
                    video, audio, project["folder_path"],
                    n_candidates=5,
                    force=True,
                    progress_callback=lambda msg, pct: self.after(
                        0, lambda m=msg: self._update_analyze_status(m),
                    ),
                )
                candidates_json = json.dumps(candidates, default=str)
                db.update_project(
                    self.conn, pid,
                    proxy_path=proxy_path,
                    spotify_candidates_json=candidates_json,
                )
                self.after(0, lambda: self._on_analysis_complete(pid))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Analysis Failed", str(e),
                ))
                self.after(0, lambda: self._reset_analyze_btn())

        threading.Thread(target=_work, daemon=True).start()


    def _on_analysis_complete(self, pid: int):
        self._reset_analyze_btn()
        self._select_project(pid)

    # ------------------------------------------------------------------
    # Interesting Image / Keyframe Extractor
    # ------------------------------------------------------------------

    def _resolve_img_source_path(self, project: dict, src_type: str) -> str:
        """Resolves the video file path based on source type selection."""
        folder = project.get("folder_path", "")
        name = project.get("name", "")

        if src_type == "yt_short":
            # 1. Rendered YouTube Short
            short_out = os.path.join(folder, f"{name} - YouTube Short.mp4")
            if os.path.isfile(short_out):
                return short_out
            # 2. Rendered FB 4K
            fb_out = os.path.join(folder, f"{name} 4k - Facebook.mp4")
            if os.path.isfile(fb_out):
                return fb_out
            # 3. Source Facebook video
            fb_src = project.get("facebook_video", "")
            if fb_src and os.path.isfile(fb_src):
                return fb_src
            return fb_src or short_out

        elif src_type == "custom":
            return getattr(self, "_img_custom_path_var", tk.StringVar()).get()

        else:  # "yt_long"
            # 1. Rendered 4K long video
            yt_out = os.path.join(folder, f"{name} 4k (Video).mp4")
            if os.path.isfile(yt_out):
                return yt_out
            # 2. Source long video
            yt_src = project.get("youtube_video", "") or project.get("video_file", "")
            if yt_src and os.path.isfile(yt_src):
                return yt_src
            return yt_src or yt_out

    def _update_img_source_info(self, video_path: str):
        """Updates duration/resolution info label for selected video."""
        if not hasattr(self, "_img_src_info_label") or not self._img_src_info_label.winfo_exists():
            return
        if not video_path or not os.path.isfile(video_path):
            self._img_src_info_label.configure(
                text="⚠️ File not found or not yet generated.",
                bootstyle="warning",
            )
            return

        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                dur = frames / fps if fps > 0 else 0.0
                cap.release()

                m, s = divmod(dur, 60.0)
                size_mb = os.path.getsize(video_path) / (1024 * 1024)
                self._img_src_info_label.configure(
                    text=f"🎬 {w}×{h} • {fps:.2f} fps • Duration: {int(m):02d}:{s:05.2f} • Size: {size_mb:.1f} MB",
                    bootstyle="success",
                )
                return
        except Exception:
            pass

        self._img_src_info_label.configure(
            text="✅ File ready for analysis.",
            bootstyle="secondary",
        )

    def _on_img_source_type_changed(self, project: dict):
        src_type = self._img_source_type_var.get()
        path = self._resolve_img_source_path(project, src_type)
        self._img_src_path_var.set(path)
        self._update_img_source_info(path)

        if hasattr(self, "_img_browse_btn"):
            if src_type == "custom":
                self._img_browse_btn.configure(state="normal")
            else:
                self._img_browse_btn.configure(state="normal")

    def _browse_img_source_file(self, project: dict):
        initialdir = project.get("folder_path", "")
        chosen = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv *.webm"), ("All files", "*.*")],
            initialdir=initialdir if os.path.isdir(initialdir) else None,
        )
        if chosen:
            self._img_source_type_var.set("custom")
            self._img_custom_path_var.set(chosen)
            self._img_src_path_var.set(chosen)
            self._update_img_source_info(chosen)

    def _build_interesting_images_tab(self, parent, project: dict):
        """Constructs the tab interface for identifying and exporting interesting images."""
        # Clean state for new project view
        self._interesting_img_thumbnails.clear()
        self._current_interesting_candidates.clear()
        self._interesting_cand_vars.clear()

        # 1. Compact Header & Video Controls Frame
        ctrl_box = ttk.LabelFrame(parent, text="Source Video & Settings")
        ctrl_box.pack(fill=X, pady=(0, 10))

        ctrl_pad = ttk.Frame(ctrl_box, padding=(10, 8, 10, 8))
        ctrl_pad.pack(fill=X)

        # Row 1: Source Radio Buttons + Browse
        row1 = ttk.Frame(ctrl_pad)
        row1.pack(fill=X, pady=(0, 5))

        self._img_source_type_var = tk.StringVar(value="yt_long")
        self._img_custom_path_var = tk.StringVar(value="")

        ttk.Radiobutton(
            row1, text="YouTube Long Video (16:9)",
            variable=self._img_source_type_var, value="yt_long",
            command=lambda: self._on_img_source_type_changed(project),
            bootstyle="info",
        ).pack(side=LEFT, padx=(0, 15))

        ttk.Radiobutton(
            row1, text="YouTube Short (9:16)",
            variable=self._img_source_type_var, value="yt_short",
            command=lambda: self._on_img_source_type_changed(project),
            bootstyle="info",
        ).pack(side=LEFT, padx=(0, 15))

        ttk.Radiobutton(
            row1, text="Custom Video…",
            variable=self._img_source_type_var, value="custom",
            command=lambda: self._on_img_source_type_changed(project),
            bootstyle="info",
        ).pack(side=LEFT, padx=(0, 10))

        self._img_browse_btn = ttk.Button(
            row1, text="Browse…", bootstyle="secondary-outline",
            command=lambda: self._browse_img_source_file(project),
        )
        self._img_browse_btn.pack(side=RIGHT)

        # Row 2: Selected path & info
        row2 = ttk.Frame(ctrl_pad)
        row2.pack(fill=X, pady=(0, 8))

        init_path = self._resolve_img_source_path(project, "yt_long")
        self._img_src_path_var = tk.StringVar(value=init_path)

        path_entry = ttk.Entry(row2, textvariable=self._img_src_path_var, state="readonly")
        path_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        self._img_src_info_label = ttk.Label(row2, text="", font=("Helvetica", 9), bootstyle="secondary")
        self._img_src_info_label.pack(side=RIGHT)
        self._update_img_source_info(init_path)

        # Row 3: Parameters & Action Button
        row3 = ttk.Frame(ctrl_pad)
        row3.pack(fill=X)

        ttk.Label(row3, text="Top Images:").pack(side=LEFT, padx=(0, 5))
        self._img_top_n_var = tk.IntVar(value=10)
        ttk.Spinbox(
            row3, from_=1, to=50, textvariable=self._img_top_n_var, width=4,
        ).pack(side=LEFT, padx=(0, 15))

        ttk.Label(row3, text="Min Sep (s):").pack(side=LEFT, padx=(0, 5))
        self._img_min_sep_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(
            row3, from_=0.5, to=60.0, increment=0.5,
            textvariable=self._img_min_sep_var, width=5,
        ).pack(side=LEFT, padx=(0, 15))

        ttk.Label(row3, text="Format:").pack(side=LEFT, padx=(0, 5))
        self._img_format_var = tk.StringVar(value="PNG (Lossless)")
        fmt_cb = ttk.Combobox(
            row3, textvariable=self._img_format_var,
            values=["PNG (Lossless)", "JPEG (High Quality)", "WEBP"],
            state="readonly", width=18,
        )
        fmt_cb.pack(side=LEFT, padx=(0, 20))

        self._img_analyze_btn = ttk.Button(
            row3, text="✨ Find Most Interesting Images", bootstyle="primary",
            command=lambda: self._start_project_image_analysis(project),
        )
        self._img_analyze_btn.pack(side=RIGHT)

        # 2. Progress Container (initially hidden)
        self._img_prog_frame = ttk.Frame(parent)
        self._img_progress_bar = ttk.Progressbar(self._img_prog_frame, mode="determinate", bootstyle="info-striped")
        self._img_progress_bar.pack(fill=X, pady=(5, 2))
        self._img_status_label = ttk.Label(self._img_prog_frame, text="", font=("Helvetica", 9), bootstyle="secondary")
        self._img_status_label.pack(anchor=W)

        # 3. Results & Gallery Container
        self._img_gallery_container = ttk.Frame(parent)
        self._img_gallery_container.pack(fill=BOTH, expand=True, pady=5)

        # Empty state prompt
        self._render_empty_gallery_state()

    def _render_empty_gallery_state(self):
        for w in self._img_gallery_container.winfo_children():
            w.destroy()
        empty_box = ttk.Frame(self._img_gallery_container, padding=20)
        empty_box.pack(fill=BOTH, expand=True)
        ttk.Label(
            empty_box,
            text="🖼️ Click '✨ Find Most Interesting Images' to scan the video.",
            font=("Helvetica", 11),
            bootstyle="secondary",
            justify=CENTER,
        ).pack(pady=15)

    def _start_project_image_analysis(self, project: dict):
        """Runs the interesting frame extraction in a background thread."""
        video_path = self._img_src_path_var.get().strip()
        if not video_path or not os.path.isfile(video_path):
            messagebox.showwarning("Missing Video", f"The selected video file does not exist:\n{video_path}")
            return

        n_top = self._img_top_n_var.get()
        min_sep = self._img_min_sep_var.get()
        project_name = project.get("name", "video")

        # Disable button, show progress
        self._img_analyze_btn.configure(state="disabled", text="Analyzing Frames…")
        self._img_prog_frame.pack(fill=X, pady=(0, 10), before=self._img_gallery_container)
        self._img_progress_bar.configure(value=0)
        self._img_status_label.configure(text="Initializing video frame scanner…")

        def _worker():
            try:
                candidates = analyze_interesting_frames(
                    video_path,
                    n_candidates=n_top,
                    min_separation=min_sep,
                    progress_callback=lambda msg, pct: self.after(
                        0, lambda m=msg, p=pct: self._update_img_progress(m, p),
                    ),
                )
                self.after(0, lambda: self._on_image_analysis_complete(candidates, video_path, project_name))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_image_analysis_error(err))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_img_progress(self, msg: str, pct: int):
        if hasattr(self, "_img_progress_bar") and self._img_progress_bar.winfo_exists():
            self._img_progress_bar.configure(value=pct)
        if hasattr(self, "_img_status_label") and self._img_status_label.winfo_exists():
            self._img_status_label.configure(text=f"{msg} ({pct}%)")

    def _on_image_analysis_error(self, err_msg: str):
        if hasattr(self, "_img_prog_frame") and self._img_prog_frame.winfo_exists():
            self._img_prog_frame.pack_forget()
        if hasattr(self, "_img_analyze_btn") and self._img_analyze_btn.winfo_exists():
            self._img_analyze_btn.configure(state="normal", text="✨ Find Most Interesting Images")
        messagebox.showerror("Analysis Error", f"Failed to analyze interesting frames:\n{err_msg}")

    def _on_image_analysis_complete(self, candidates: list[dict], video_path: str, project_name: str):
        if hasattr(self, "_img_prog_frame") and self._img_prog_frame.winfo_exists():
            self._img_prog_frame.pack_forget()
        if hasattr(self, "_img_analyze_btn") and self._img_analyze_btn.winfo_exists():
            self._img_analyze_btn.configure(state="normal", text="✨ Re-analyze Images")

        self._current_interesting_candidates = candidates
        self._render_interesting_images_gallery(self._img_gallery_container, candidates, video_path, project_name)

    def _render_interesting_images_gallery(self, container, candidates: list[dict], video_path: str, project_name: str):
        """Renders the top action toolbar and visual grid cards for extracted candidates."""
        for w in container.winfo_children():
            w.destroy()

        if not candidates:
            ttk.Label(
                container, text="No interesting frames met the criteria.",
                font=("Helvetica", 11), bootstyle="warning",
            ).pack(pady=20)
            return

        # 1. Action Toolbar
        toolbar = ttk.Frame(container)
        toolbar.pack(fill=X, pady=(0, 10))

        # Checkbox states
        self._interesting_cand_vars.clear()
        for c in candidates:
            self._interesting_cand_vars[c["rank"]] = tk.BooleanVar(value=True)

        self._img_count_label = ttk.Label(
            toolbar,
            text=f"✨ Found {len(candidates)} Top Frames ({len(candidates)} selected)",
            font=("Helvetica", 11, "bold"),
            bootstyle="success",
        )
        self._img_count_label.pack(side=LEFT, padx=(0, 15))

        ttk.Button(
            toolbar, text="Select All", bootstyle="secondary-outline",
            command=lambda: self._toggle_all_image_selections(True),
        ).pack(side=LEFT, padx=(0, 5))

        ttk.Button(
            toolbar, text="Deselect All", bootstyle="secondary-outline",
            command=lambda: self._toggle_all_image_selections(False),
        ).pack(side=LEFT, padx=(0, 15))

        ttk.Button(
            toolbar, text="💾 Export Selected…", bootstyle="success",
            command=lambda: self._export_selected_images(video_path, project_name),
        ).pack(side=RIGHT, padx=(5, 0))

        ttk.Button(
            toolbar, text="💾 Export All…", bootstyle="success-outline",
            command=lambda: self._export_all_images(video_path, project_name),
        ).pack(side=RIGHT, padx=(5, 0))

        # 2. Card Gallery Grid
        cards_frame = ttk.Frame(container)
        cards_frame.pack(fill=BOTH, expand=True)

        # Detect aspect ratio to choose optimal column layout
        is_vertical = False
        if candidates and candidates[0].get("height", 0) > candidates[0].get("width", 0):
            is_vertical = True

        cols = 3 if is_vertical else 2
        for col_idx in range(cols):
            cards_frame.columnconfigure(col_idx, weight=1, uniform="col")

        max_thumb_size = 220 if is_vertical else 280

        for idx, cand in enumerate(candidates):
            row_idx = idx // cols
            col_idx = idx % cols

            rank = cand["rank"]
            t_sec = cand["timestamp_sec"]
            score = cand["overall_score"]
            time_str = cand["time_str"]
            w_px = cand["width"]
            h_px = cand["height"]

            card = ttk.LabelFrame(
                cards_frame,
                text=f"  Rank #{rank}  (Score: {score:.1f}/100)  ",
            )
            card.grid(row=row_idx, column=col_idx, padx=6, pady=6, sticky="nsew")

            # Inner padding container
            card_pad = ttk.Frame(card, padding=8)
            card_pad.pack(fill=BOTH, expand=True)

            # Header inside card
            head_row = ttk.Frame(card_pad)
            head_row.pack(fill=X, pady=(0, 4))

            chk = ttk.Checkbutton(
                head_row,
                text=f"Select",
                variable=self._interesting_cand_vars[rank],
                command=self._on_image_checkbox_toggled,
                bootstyle="success-square-toggle",
            )
            chk.pack(side=LEFT)

            ttk.Label(
                head_row,
                text=f"⏱ {time_str} • {w_px}×{h_px}",
                font=("Helvetica", 9, "bold"),
                bootstyle="info",
            ).pack(side=RIGHT)

            # Thumbnail Image Preview (pre-extracted or on-demand)
            thumb_key = f"{video_path}_{t_sec}_{rank}"
            pil_thumb = cand.get("thumbnail_pil") or extract_thumbnail_image(video_path, t_sec, max_dim=max_thumb_size)
            if pil_thumb:
                tk_thumb = ImageTk.PhotoImage(pil_thumb)
                self._interesting_img_thumbnails[thumb_key] = tk_thumb

                img_lbl = ttk.Label(card_pad, image=tk_thumb, cursor="hand2")
                img_lbl.image = tk_thumb
                img_lbl.pack(pady=3)
                img_lbl.bind(
                    "<Button-1>",
                    lambda e, v=video_path, t=t_sec, r=rank, s=score: self._show_fullsize_image_preview(
                        v, t, f"Rank #{r} ({time_str}, Score {s:.1f})"
                    ),
                )
            else:
                ttk.Label(card_pad, text="[No Preview Available]", bootstyle="secondary").pack(pady=20)

            # Metrics row
            metrics_text = (
                f"Sharp: {cand['sharpness']:.0f}  |  "
                f"Color: {cand['colorfulness']:.1f}  |  "
                f"Contrast: {cand['contrast']:.1f}  |  "
                f"Entropy: {cand['entropy']:.2f}"
            )
            ttk.Label(
                card_pad, text=metrics_text, font=("Helvetica", 8), bootstyle="secondary",
            ).pack(pady=(2, 4))

            # Action buttons
            btn_row = ttk.Frame(card_pad)
            btn_row.pack(fill=X, pady=(4, 0))

            ttk.Button(
                btn_row, text="👁 Preview", bootstyle="info-outline", width=10,
                command=lambda v=video_path, t=t_sec, r=rank, s=score: self._show_fullsize_image_preview(
                    v, t, f"Rank #{r} ({time_str}, Score {s:.1f})"
                ),
            ).pack(side=LEFT)

            default_fn = f"{project_name}_rank{rank:02d}_{cand['time_str'].replace(':', 'm').replace('.', 's')}"
            ttk.Button(
                btn_row, text="💾 Save…", bootstyle="secondary-outline", width=8,
                command=lambda v=video_path, c=cand, df=default_fn: self._export_single_image(v, c, df),
            ).pack(side=RIGHT)

        # Force geometry refresh and mousewheel scrolling on all newly rendered elements
        self.update_idletasks()
        if hasattr(self, "_detail_content") and self._detail_content and hasattr(self._detail_content, "enable_scrolling"):
            try:
                self._detail_content.enable_scrolling()
            except Exception:
                pass

    def _toggle_all_image_selections(self, select: bool):
        for var in self._interesting_cand_vars.values():
            var.set(select)
        self._on_image_checkbox_toggled()

    def _on_image_checkbox_toggled(self):
        if hasattr(self, "_img_count_label") and self._img_count_label.winfo_exists():
            selected_count = sum(1 for v in self._interesting_cand_vars.values() if v.get())
            total = len(self._interesting_cand_vars)
            self._img_count_label.configure(
                text=f"✨ Found {total} Top Frames ({selected_count} selected)"
            )

    def _get_export_format_ext(self) -> str:
        fmt_str = getattr(self, "_img_format_var", tk.StringVar(value="PNG")).get()
        if "JPEG" in fmt_str or "JPG" in fmt_str:
            return "jpg"
        elif "WEBP" in fmt_str:
            return "webp"
        return "png"

    def _export_single_image(self, video_path: str, candidate: dict, default_name: str):
        """Export a single frame with a Save As file dialog."""
        ext = self._get_export_format_ext()
        file_path = filedialog.asksaveasfilename(
            title="Save Image As",
            initialfile=f"{default_name}.{ext}",
            defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} Image", f"*.{ext}"), ("All files", "*.*")],
        )
        if not file_path:
            return

        ok = export_frame(video_path, candidate["timestamp_sec"], file_path, img_format=ext)
        if ok:
            messagebox.showinfo("Export Successful", f"Saved full-resolution image to:\n{file_path}")
        else:
            messagebox.showerror("Export Failed", "Failed to extract and save the image.")

    def _export_selected_images(self, video_path: str, project_name: str):
        """Export all currently checked frames to a chosen directory."""
        selected_candidates = [
            c for c in self._current_interesting_candidates
            if self._interesting_cand_vars.get(c["rank"], tk.BooleanVar()).get()
        ]
        if not selected_candidates:
            messagebox.showwarning("No Selection", "Please select at least one image to export.")
            return

        self._run_batch_export_dialog(video_path, selected_candidates, project_name)

    def _export_all_images(self, video_path: str, project_name: str):
        """Export all analyzed candidate frames to a chosen directory."""
        if not self._current_interesting_candidates:
            messagebox.showwarning("No Images", "No analyzed images available to export.")
            return
        self._run_batch_export_dialog(video_path, self._current_interesting_candidates, project_name)

    def _run_batch_export_dialog(self, video_path: str, candidates: list[dict], project_name: str):
        export_dir = filedialog.askdirectory(title="Select Folder to Export Images")
        if not export_dir:
            return

        ext = self._get_export_format_ext()
        saved = batch_export_frames(
            video_path,
            candidates,
            output_dir=export_dir,
            prefix=project_name,
            img_format=ext,
        )

        if saved:
            res = messagebox.askyesno(
                "Export Complete",
                f"Successfully exported {len(saved)} image(s) to:\n{export_dir}\n\nOpen export folder in Finder/Explorer?",
            )
            if res:
                self._open_in_file_manager(export_dir)
        else:
            messagebox.showerror("Export Failed", "Failed to export images.")

    def _show_fullsize_image_preview(self, video_path: str, timestamp_sec: float, title_info: str):
        """Opens a modal window showing the full-resolution extracted frame."""
        rgb = extract_full_frame(video_path, timestamp_sec)
        if rgb is None:
            messagebox.showerror("Error", "Could not extract full-resolution frame.")
            return

        h, w = rgb.shape[:2]

        dlg = ttk.Toplevel(self)
        dlg.title(f"Frame Preview — {title_info}")
        dlg.geometry("1100x780")
        dlg.minsize(700, 500)
        dlg.transient(self)

        top_bar = ttk.Frame(dlg, padding=10)
        top_bar.pack(fill=X)

        ttk.Label(
            top_bar,
            text=f"🖼️ {title_info}",
            font=("Helvetica", 12, "bold"),
            bootstyle="inverse-dark",
        ).pack(side=LEFT)

        ttk.Label(
            top_bar,
            text=f"Original Resolution: {w} × {h} px",
            font=("Helvetica", 10),
            bootstyle="secondary",
        ).pack(side=LEFT, padx=15)

        ext = self._get_export_format_ext()
        default_save_name = f"frame_{int(timestamp_sec)}s.{ext}"

        ttk.Button(
            top_bar, text="💾 Save Full-Resolution Image…", bootstyle="success",
            command=lambda: self._export_single_image(
                video_path, {"timestamp_sec": timestamp_sec}, default_save_name
            ),
        ).pack(side=RIGHT)

        canvas = tk.Canvas(dlg, bg="#0d0d1a", highlightthickness=0)
        canvas.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        pil_orig = Image.fromarray(rgb)
        dlg._preview_photo = None  # prevent GC

        def _update_canvas_img(event=None):
            cw = canvas.winfo_width()
            ch = canvas.winfo_height()
            if cw < 10 or ch < 10:
                cw, ch = 1000, 650
            scale = min(cw / w, ch / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            resized = pil_orig.resize((nw, nh), Image.Resampling.LANCZOS)
            dlg._preview_photo = ImageTk.PhotoImage(resized)
            canvas.delete("all")
            canvas.create_image(cw // 2, ch // 2, anchor="center", image=dlg._preview_photo)

        canvas.bind("<Configure>", _update_canvas_img)
        dlg.after(50, _update_canvas_img)

    def _open_standalone_frame_extractor(self):
        """Opens a standalone modal dialog allowing interesting image extraction from ANY video file."""
        dlg = ttk.Toplevel(self)
        dlg.title("🖼️ Frame & Image Extractor")
        dlg.geometry("1200x850")
        dlg.minsize(900, 600)
        dlg.transient(self)

        content = ScrolledFrame(dlg, autohide=True)
        content.pack(fill=BOTH, expand=True, padx=15, pady=15)

        ttk.Label(
            content,
            text="🖼️ Video Frame & Interesting Image Extractor",
            font=("Helvetica", 16, "bold"),
            bootstyle="inverse-dark",
        ).pack(anchor=W, pady=(0, 5))

        ttk.Label(
            content,
            text="Select any YouTube long video, short, or local media file to extract its most interesting, sharp, and colorful frames.",
            font=("Helvetica", 10),
            bootstyle="secondary",
        ).pack(anchor=W, pady=(0, 15))

        # File Selection Frame
        file_lf = ttk.LabelFrame(content, text="Select Video File")
        file_lf.pack(fill=X, pady=(0, 10))

        file_var = tk.StringVar()
        file_row = ttk.Frame(file_lf, padding=(10, 5, 10, 0))
        file_row.pack(fill=X, pady=(0, 5))

        file_entry = ttk.Entry(file_row, textvariable=file_var)
        file_entry.pack(side=LEFT, fill=X, expand=True, padx=(0, 8))

        info_lbl = ttk.Label(file_lf, text="", font=("Helvetica", 9), padding=(10, 0, 10, 8))
        info_lbl.pack(anchor=W)

        def _update_standalone_info(path):
            if not path or not os.path.isfile(path):
                info_lbl.configure(text="⚠️ Please select a valid video file.", bootstyle="warning")
                return
            try:
                import cv2
                cap = cv2.VideoCapture(path)
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    dur = frames / fps if fps > 0 else 0.0
                    cap.release()
                    m, s = divmod(dur, 60.0)
                    size_mb = os.path.getsize(path) / (1024 * 1024)
                    info_lbl.configure(
                        text=f"🎬 {w}×{h} • {fps:.2f} fps • Duration: {int(m):02d}:{s:05.2f} • Size: {size_mb:.1f} MB",
                        bootstyle="success",
                    )
            except Exception:
                info_lbl.configure(text="✅ Ready for analysis.", bootstyle="secondary")

        def _browse_standalone():
            chosen = filedialog.askopenfilename(
                title="Select Video File",
                filetypes=[("Video files", "*.mp4 *.mov *.avi *.mkv *.webm"), ("All files", "*.*")],
            )
            if chosen:
                file_var.set(chosen)
                _update_standalone_info(chosen)

        ttk.Button(
            file_row, text="Browse…", bootstyle="primary-outline",
            command=_browse_standalone,
        ).pack(side=RIGHT)

        # Settings Frame
        set_lf = ttk.LabelFrame(content, text="Extraction Settings")
        set_lf.pack(fill=X, pady=(0, 10))

        set_row = ttk.Frame(set_lf, padding=(10, 8, 10, 8))
        set_row.pack(fill=X, pady=5)

        ttk.Label(set_row, text="Top Images:").pack(side=LEFT, padx=(0, 5))
        top_n_var = tk.IntVar(value=10)
        ttk.Spinbox(set_row, from_=1, to=50, textvariable=top_n_var, width=5).pack(side=LEFT, padx=(0, 20))

        ttk.Label(set_row, text="Min Separation (s):").pack(side=LEFT, padx=(0, 5))
        min_sep_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(set_row, from_=0.5, to=60.0, increment=0.5, textvariable=min_sep_var, width=5).pack(side=LEFT, padx=(0, 20))

        ttk.Label(set_row, text="Format:").pack(side=LEFT, padx=(0, 5))
        fmt_var = tk.StringVar(value="PNG (Lossless)")
        ttk.Combobox(
            set_row, textvariable=fmt_var, values=["PNG (Lossless)", "JPEG (High Quality)", "WEBP"],
            state="readonly", width=18,
        ).pack(side=LEFT, padx=(0, 25))

        prog_frame = ttk.Frame(content)
        prog_bar = ttk.Progressbar(prog_frame, mode="determinate", bootstyle="info-striped")
        prog_bar.pack(fill=X, pady=(5, 2))
        prog_status = ttk.Label(prog_frame, text="", font=("Helvetica", 9), bootstyle="secondary")
        prog_status.pack(anchor=W)

        gallery_container = ttk.Frame(content)
        gallery_container.pack(fill=BOTH, expand=True, pady=10)

        def _start_analysis():
            vpath = file_var.get().strip()
            if not vpath or not os.path.isfile(vpath):
                messagebox.showwarning("Missing File", "Please select a valid video file.")
                return

            analyze_btn.configure(state="disabled", text="Analyzing…")
            prog_frame.pack(fill=X, pady=(0, 10), before=gallery_container)
            prog_bar.configure(value=0)
            prog_status.configure(text="Scanning frames…")

            def _worker():
                try:
                    cands = analyze_interesting_frames(
                        vpath,
                        n_candidates=top_n_var.get(),
                        min_separation=min_sep_var.get(),
                        progress_callback=lambda msg, pct: dlg.after(
                            0, lambda m=msg, p=pct: (prog_bar.configure(value=p), prog_status.configure(text=f"{m} ({p}%)")),
                        ),
                    )
                    dlg.after(0, lambda: _on_complete(cands, vpath))
                except Exception as e:
                    dlg.after(0, lambda err=str(e): _on_err(err))

            threading.Thread(target=_worker, daemon=True).start()

        def _on_err(err):
            prog_frame.pack_forget()
            analyze_btn.configure(state="normal", text="✨ Analyze & Extract Images")
            messagebox.showerror("Error", err)

        def _on_complete(cands, vpath):
            prog_frame.pack_forget()
            analyze_btn.configure(state="normal", text="✨ Re-analyze Images")
            base_name = os.path.splitext(os.path.basename(vpath))[0]
            self._current_interesting_candidates = cands
            self._render_interesting_images_gallery(gallery_container, cands, vpath, base_name)
            dlg.update_idletasks()
            if hasattr(content, "enable_scrolling"):
                try:
                    content.enable_scrolling()
                except Exception:
                    pass

        analyze_btn = ttk.Button(
            set_row, text="✨ Analyze & Extract Images", bootstyle="primary",
            command=_start_analysis,
        )
        analyze_btn.pack(side=RIGHT)

    # ------------------------------------------------------------------
    # Video Player
    # ------------------------------------------------------------------

    def _play_preview(self, path: str):
        """Load and play a preview clip in the embedded player."""
        if hasattr(self, "_detail_notebook") and hasattr(self, "_tab_pub"):
            try:
                self._detail_notebook.select(self._tab_pub)
            except Exception:
                pass
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
        for status in ["processing", "deploying", "queued", "staged", "pending_deployment", "error", "done"]:
            iid = f"group_{status}"
            if self._tree.exists(iid):
                open_states[status] = bool(self._tree.item(iid, "open"))
            else:
                # Default states
                open_states[status] = status in ("staged", "queued", "processing", "deploying", "pending_deployment", "error")

        self._tree.delete(*self._tree.get_children())
        projects = db.get_all_projects(self.conn)

        filter_text = self._filter_var.get().lower()

        # Group by status
        groups = {
            "processing": [],
            "deploying": [],
            "queued": [],
            "staged": [],
            "pending_deployment": [],
            "error": [],
            "done": [],
        }
        for p in projects:
            s = p["status"]
            if s not in groups:
                groups[s] = []
            groups[s].append(p)

        # Sort the queued group by execution order: oldest queued first
        groups["queued"].sort(key=lambda x: (x.get("queued_at") or x.get("created_at") or "", x.get("id", 0)))

        for status, items in groups.items():
            if not items:
                continue
            label = f"  {STATUS_ICONS.get(status, '')}  {status.upper()}  ({len(items)})"
            group_id = self._tree.insert(
                "", "end", iid=f"group_{status}", text=label,
                tags=(f"group_{status}",),
                open=open_states.get(status, status in ("staged", "queued", "processing", "deploying", "pending_deployment", "error")),
            )

            for p in items:
                if filter_text and filter_text not in p["name"].lower():
                    continue
                display = p["name"]
                if status == "processing":
                    step = p.get("current_step", 0)
                    display += f"  (Step {step}/9)"
                elif status == "deploying":
                    step = p.get("current_step", 0)
                    display += f"  (Step {step}/12)"
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
        self._tree.tag_configure("group_pending_deployment", foreground="#5bc0de")
        self._tree.tag_configure("group_deploying", foreground="#5bc0de")
        self._tree.tag_configure("group_done", foreground="#5cb85c")
        self._tree.tag_configure("group_error", foreground="#d9534f")
        self._tree.tag_configure("item_staged", foreground="#adb5bd")
        self._tree.tag_configure("item_queued", foreground="#f0ad4e")
        self._tree.tag_configure("item_processing", foreground="#5bc0de")
        self._tree.tag_configure("item_pending_deployment", foreground="#5bc0de")
        self._tree.tag_configure("item_deploying", foreground="#5bc0de")
        self._tree.tag_configure("item_done", foreground="#5cb85c")
        self._tree.tag_configure("item_error", foreground="#d9534f")
        self._tree.tag_configure("reorder_arrow", foreground="#cccccc")

    # ------------------------------------------------------------------
    # Queue Reorder (inline column click)
    # ------------------------------------------------------------------

    def _on_tree_click(self, event):
        """Detect clicks on the 'reorder' column and move the queued item up/down."""
        col = self._tree.identify_column(event.x)
        if col != "#1":  # #1 = first data column = 'reorder'
            return
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        tags = self._tree.item(iid, "tags")
        if "item_queued" not in tags:
            return
        # Left half of the column = up, right half = down
        col_x = self._tree.column("reorder", "x")
        col_w = self._tree.column("reorder", "width")
        mid = col_x + col_w // 2
        direction = -1 if event.x < mid else 1
        self._reorder_queue_item(iid, direction)

    def _reorder_queue_item(self, iid: str, direction: int):
        """Move a queued project up (-1) or down (+1) by swapping queued_at timestamps."""
        values = self._tree.item(iid, "values")
        if not values:
            return
        try:
            pid = int(values[0])
        except (ValueError, IndexError):
            return

        queued = db.get_projects_by_status(self.conn, "queued")
        ids = [p["id"] for p in queued]
        if pid not in ids:
            return

        idx = ids.index(pid)
        swap_idx = idx + direction
        if swap_idx < 0 or swap_idx >= len(ids):
            return  # Already at top/bottom

        p_a = queued[idx]
        p_b = queued[swap_idx]
        ts_a = p_a.get("queued_at") or p_a.get("created_at") or datetime.now().isoformat()
        ts_b = p_b.get("queued_at") or p_b.get("created_at") or datetime.now().isoformat()

        if ts_a == ts_b:
            ts_a = (datetime.fromisoformat(ts_a) - timedelta(milliseconds=1)).isoformat()

        db.update_project(self.conn, p_a["id"], queued_at=ts_b)
        db.update_project(self.conn, p_b["id"], queued_at=ts_a)
        self._refresh_list()

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
        dlg.geometry("700x650")
        dlg.transient(self)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=15)
        f.pack(fill=BOTH, expand=True)

        ttk.Label(
            f, text="Settings",
            font=("Helvetica", 14, "bold"),
        ).pack(anchor=W, pady=(0, 10))

        notebook = ttk.Notebook(f)
        notebook.pack(fill=BOTH, expand=True, pady=10)

        # Tab 1: General Settings
        tab_gen = ttk.Frame(notebook, padding=10)
        notebook.add(tab_gen, text="General Settings")

        # Tab 2: Facebook Integration
        tab_fb = ttk.Frame(notebook, padding=10)
        notebook.add(tab_fb, text="Facebook Integration")

        # Tab 3: YouTube Integration
        tab_yt = ttk.Frame(notebook, padding=10)
        notebook.add(tab_yt, text="YouTube Integration")

        # Tab 4: TikTok Integration
        tab_tiktok = ttk.Frame(notebook, padding=10)
        notebook.add(tab_tiktok, text="TikTok Integration")

        # Tab 5: Gemini
        tab_gemini = ttk.Frame(notebook, padding=10)
        notebook.add(tab_gemini, text="Gemini")

        # Tab 6: Default Templates
        tab_tpl_container = ttk.Frame(notebook, padding=10)
        notebook.add(tab_tpl_container, text="Default Templates")
        tab_tpl = ScrolledFrame(tab_tpl_container, autohide=True)
        tab_tpl.pack(fill=BOTH, expand=True)

        # --- TAB 1: General Settings ---
        wf_frame = ttk.Frame(tab_gen)
        wf_frame.pack(fill=X, pady=8)
        ttk.Label(wf_frame, text="Watch Folder:", width=18, anchor=W).pack(side=LEFT)
        watch_var = tk.StringVar(value=db.get_setting(self.conn, "watch_folder", ""))
        ttk.Entry(wf_frame, textvariable=watch_var).pack(side=LEFT, fill=X, expand=True, padx=5)
        ttk.Button(wf_frame, text="Browse", bootstyle="secondary-outline",
                   command=lambda: watch_var.set(filedialog.askdirectory(title="Select Watch Folder") or watch_var.get())
        ).pack(side=RIGHT)

        # --- TAB 4: Gemini Settings ---
        ttk.Label(tab_gemini, text="Gemini Integration Settings", font=("Helvetica", 11, "bold")).pack(anchor=W, pady=(0, 10))

        gemini_key_frame = ttk.Frame(tab_gemini)
        gemini_key_frame.pack(fill=X, pady=8)
        ttk.Label(gemini_key_frame, text="Gemini API Key:", width=18, anchor=W).pack(side=LEFT)
        gemini_key_var = tk.StringVar(value=db.get_setting(self.conn, "gemini_api_key", ""))
        ttk.Entry(gemini_key_frame, textvariable=gemini_key_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        gemini_model_frame = ttk.Frame(tab_gemini)
        gemini_model_frame.pack(fill=X, pady=8)
        ttk.Label(gemini_model_frame, text="Gemini Model:", width=18, anchor=W).pack(side=LEFT)
        gemini_model_var = tk.StringVar(value=db.get_setting(self.conn, "gemini_model", "gemini-2.5-flash"))
        ttk.Entry(gemini_model_frame, textvariable=gemini_model_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        # --- TAB 2: Facebook Integration ---
        ttk.Label(tab_fb, text="Facebook Page Integration Settings", font=("Helvetica", 11, "bold")).pack(anchor=W, pady=(0, 10))

        fb_pid_frame = ttk.Frame(tab_fb)
        fb_pid_frame.pack(fill=X, pady=5)
        ttk.Label(fb_pid_frame, text="FB Page ID:", width=18, anchor=W).pack(side=LEFT)
        fb_page_id_var = tk.StringVar(value=db.get_setting(self.conn, "fb_page_id", ""))
        ttk.Entry(fb_pid_frame, textvariable=fb_page_id_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        fb_tok_frame = ttk.Frame(tab_fb)
        fb_tok_frame.pack(fill=X, pady=5)
        ttk.Label(fb_tok_frame, text="FB Page Token:", width=18, anchor=W).pack(side=LEFT)
        fb_token_var = tk.StringVar(value=db.get_setting(self.conn, "fb_page_token", ""))
        ttk.Entry(fb_tok_frame, textvariable=fb_token_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        fb_long_user_tok_frame = ttk.Frame(tab_fb)
        fb_long_user_tok_frame.pack(fill=X, pady=5)
        ttk.Label(fb_long_user_tok_frame, text="FB Long User Token:", width=18, anchor=W).pack(side=LEFT)
        fb_long_user_token_var = tk.StringVar(value=db.get_setting(self.conn, "fb_long_user_token", ""))
        ttk.Entry(fb_long_user_tok_frame, textvariable=fb_long_user_token_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        # Exchange Section
        ttk.Separator(tab_fb, orient="horizontal").pack(fill=X, pady=15)
        ttk.Label(tab_fb, text="Exchange Short-Lived User Token for Long-Lived Token", font=("Helvetica", 11, "bold")).pack(anchor=W, pady=(0, 10))

        fb_appid_frame = ttk.Frame(tab_fb)
        fb_appid_frame.pack(fill=X, pady=5)
        ttk.Label(fb_appid_frame, text="FB App ID:", width=18, anchor=W).pack(side=LEFT)
        fb_app_id_var = tk.StringVar(value=db.get_setting(self.conn, "fb_app_id", ""))
        ttk.Entry(fb_appid_frame, textvariable=fb_app_id_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        fb_secret_frame = ttk.Frame(tab_fb)
        fb_secret_frame.pack(fill=X, pady=5)
        ttk.Label(fb_secret_frame, text="FB App Secret:", width=18, anchor=W).pack(side=LEFT)
        fb_app_secret_var = tk.StringVar(value=db.get_setting(self.conn, "fb_app_secret", ""))
        ttk.Entry(fb_secret_frame, textvariable=fb_app_secret_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        fb_short_tok_frame = ttk.Frame(tab_fb)
        fb_short_tok_frame.pack(fill=X, pady=5)
        ttk.Label(fb_short_tok_frame, text="Short-Lived Token:", width=18, anchor=W).pack(side=LEFT)
        fb_short_token_var = tk.StringVar()
        ttk.Entry(fb_short_tok_frame, textvariable=fb_short_token_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        def exchange_token_action():
            app_id = fb_app_id_var.get().strip()
            app_secret = fb_app_secret_var.get().strip()
            short_token = fb_short_token_var.get().strip()
            page_id = fb_page_id_var.get().strip()

            if not app_id or not app_secret or not short_token:
                messagebox.showwarning("Missing Fields", "Please enter App ID, App Secret, and Short-Lived Token.")
                return

            exchange_btn.configure(state="disabled", text="Exchanging...")

            def _thread_exchange():
                from pipeline.upload import exchange_facebook_token
                long_user, page_token, err = exchange_facebook_token(app_id, app_secret, short_token, page_id)

                def done():
                    exchange_btn.configure(state="normal", text="Get Long-Lived Token")
                    if err:
                        messagebox.showerror("Exchange Failed", err)
                    else:
                        fb_long_user_token_var.set(long_user)
                        if page_token:
                            fb_token_var.set(page_token)
                        messagebox.showinfo("Success", "Successfully exchanged and populated tokens!")

                self.after(0, done)

            threading.Thread(target=_thread_exchange, daemon=True).start()

        def run_oauth_action():
            app_id = fb_app_id_var.get().strip()
            app_secret = fb_app_secret_var.get().strip()
            page_id = fb_page_id_var.get().strip()

            if not app_id or not app_secret:
                messagebox.showwarning("Missing Fields", "Please enter App ID and App Secret first.")
                return

            login_btn.configure(state="disabled", text="Logging in...")

            def _thread_login():
                from pipeline.upload import run_facebook_oauth_flow
                short_token, long_user, page_token, err = run_facebook_oauth_flow(app_id, app_secret, page_id)

                def done():
                    login_btn.configure(state="normal", text="Login with Facebook")
                    if err:
                        messagebox.showerror("Login Failed", err)
                    else:
                        fb_short_token_var.set(short_token)
                        fb_long_user_token_var.set(long_user)
                        if page_token:
                            fb_token_var.set(page_token)
                        messagebox.showinfo("Success", "Successfully logged in and retrieved tokens!")

                self.after(0, done)

            threading.Thread(target=_thread_login, daemon=True).start()

        def get_page_token_action():
            page_id = fb_page_id_var.get().strip()
            user_token = fb_short_token_var.get().strip() or fb_long_user_token_var.get().strip() or fb_token_var.get().strip()

            if not page_id or not user_token:
                messagebox.showwarning("Missing Fields", "Please enter FB Page ID and either a Short-Lived Token, Long User Token, or Access Token.")
                return

            page_token_btn.configure(state="disabled", text="Fetching...")

            def _thread_get_page_token():
                from pipeline.upload import get_facebook_page_token
                token, err = get_facebook_page_token(user_token, page_id)

                def done():
                    page_token_btn.configure(state="normal", text="Get Page Token")
                    if err:
                        messagebox.showerror("Failed to Get Page Token", err)
                    else:
                        fb_token_var.set(token)
                        messagebox.showinfo("Success", "Successfully retrieved and populated Page Access Token!")

                self.after(0, done)

            threading.Thread(target=_thread_get_page_token, daemon=True).start()

        exchange_btn_frame = ttk.Frame(tab_fb)
        exchange_btn_frame.pack(fill=X, pady=10)
        
        page_token_btn = ttk.Button(exchange_btn_frame, text="Get Page Token", bootstyle="info-outline", command=get_page_token_action)
        page_token_btn.pack(side=RIGHT, padx=5)

        exchange_btn = ttk.Button(exchange_btn_frame, text="Get Long-Lived Token", bootstyle="primary-outline", command=exchange_token_action)
        exchange_btn.pack(side=RIGHT, padx=5)

        login_btn = ttk.Button(exchange_btn_frame, text="Login with Facebook", bootstyle="primary", command=run_oauth_action)
        login_btn.pack(side=RIGHT, padx=5)



        fb_tip_lbl = ttk.Label(
            tab_fb,
            text="💡 Tip: Short-lived user tokens expire in ~2 hours. Exchanging them will retrieve a long-lived user token (valid for 60 days) or a never-expiring Page Access Token if a Page ID is specified.",
            font=("Helvetica", 8, "italic"),
            bootstyle="secondary",
            justify=LEFT,
            wraplength=650
        )
        fb_tip_lbl.pack(anchor=W, pady=(5, 10))

        # --- TAB 3: YouTube Integration ---
        ttk.Label(tab_yt, text="YouTube Data API v3 OAuth Settings", font=("Helvetica", 11, "bold")).pack(anchor=W, pady=(0, 10))

        yt_sec_frame = ttk.Frame(tab_yt)
        yt_sec_frame.pack(fill=X, pady=8)
        ttk.Label(yt_sec_frame, text="Client Secrets JSON:", width=18, anchor=W).pack(side=LEFT)
        yt_secrets_var = tk.StringVar(value=db.get_setting(self.conn, "yt_client_secrets", ""))
        
        secrets_status_var = tk.StringVar()
        def update_secrets_status():
            js = yt_secrets_var.get().strip()
            if js:
                try:
                    data = json.loads(js)
                    client_type = "web" if "web" in data else "installed" if "installed" in data else "unknown"
                    client_id = data.get(client_type, {}).get("client_id", "unknown")
                    secrets_status_var.set(f"Loaded ({client_type}, ID: ...{client_id[-8:]})")
                except Exception:
                    secrets_status_var.set("Invalid JSON format!")
            else:
                secrets_status_var.set("No client secrets loaded.")
        
        update_secrets_status()
        ttk.Label(yt_sec_frame, textvariable=secrets_status_var, font=("Helvetica", 9, "bold"), bootstyle="secondary").pack(side=LEFT, padx=5)
        
        def load_secrets_file():
            path = filedialog.askopenfilename(title="Select client_secrets.json", filetypes=[("JSON files", "*.json")])
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        content = file.read()
                    json.loads(content)
                    yt_secrets_var.set(content)
                    update_secrets_status()
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to parse JSON: {e}")
                    
        ttk.Button(yt_sec_frame, text="Load File", bootstyle="info-outline", command=load_secrets_file).pack(side=RIGHT)

        yt_auth_frame = ttk.Frame(tab_yt)
        yt_auth_frame.pack(fill=X, pady=15)
        
        ttk.Label(yt_auth_frame, text="Authorization Status:", width=18, anchor=W).pack(side=LEFT)
        
        yt_auth_status_var = tk.StringVar()
        def check_yt_status():
            from pipeline.upload import get_youtube_client
            creds, err = get_youtube_client(self.conn)
            if creds:
                yt_auth_status_var.set("Authorized ✅")
            else:
                yt_auth_status_var.set("Not Authorized ❌")
                
        check_yt_status()
        
        status_lbl = ttk.Label(yt_auth_frame, textvariable=yt_auth_status_var, font=("Helvetica", 10, "bold"))
        status_lbl.pack(side=LEFT, padx=5)
        
        def update_status_style(*args):
            val = yt_auth_status_var.get()
            if "✅" in val:
                status_lbl.configure(bootstyle="success")
            else:
                status_lbl.configure(bootstyle="danger")
                
        yt_auth_status_var.trace_add("write", update_status_style)
        update_status_style()
        
        def run_auth():
            secrets_json = yt_secrets_var.get().strip()
            if not secrets_json:
                messagebox.showwarning("Missing Secrets", "Please load a Client Secrets JSON file first.")
                return
                
            auth_btn.configure(state="disabled", text="Authorizing...")
            
            def _thread_auth():
                from pipeline.upload import run_youtube_oauth_flow
                success, msg = run_youtube_oauth_flow(self.conn, secrets_json, log_callback=print)
                
                def done():
                    auth_btn.configure(state="normal", text="Authorize YouTube")
                    if success:
                        messagebox.showinfo("Success", msg)
                    else:
                        messagebox.showerror("Failed", msg)
                    check_yt_status()
                    
                self.after(0, done)
                
            threading.Thread(target=_thread_auth, daemon=True).start()
            
        auth_btn = ttk.Button(yt_auth_frame, text="Authorize YouTube", bootstyle="success", command=run_auth)
        auth_btn.pack(side=RIGHT)

        # --- TAB: TikTok Integration ---
        ttk.Label(tab_tiktok, text="TikTok Content Posting API Settings", font=("Helvetica", 11, "bold")).pack(anchor=W, pady=(0, 10))

        tt_ck_frame = ttk.Frame(tab_tiktok)
        tt_ck_frame.pack(fill=X, pady=5)
        ttk.Label(tt_ck_frame, text="Client Key:", width=18, anchor=W).pack(side=LEFT)
        tiktok_client_key_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_client_key", ""))
        ttk.Entry(tt_ck_frame, textvariable=tiktok_client_key_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_cs_frame = ttk.Frame(tab_tiktok)
        tt_cs_frame.pack(fill=X, pady=5)
        ttk.Label(tt_cs_frame, text="Client Secret:", width=18, anchor=W).pack(side=LEFT)
        tiktok_client_secret_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_client_secret", ""))
        ttk.Entry(tt_cs_frame, textvariable=tiktok_client_secret_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_tok_frame = ttk.Frame(tab_tiktok)
        tt_tok_frame.pack(fill=X, pady=5)
        ttk.Label(tt_tok_frame, text="Access Token:", width=18, anchor=W).pack(side=LEFT)
        tiktok_token_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_access_token", ""))
        ttk.Entry(tt_tok_frame, textvariable=tiktok_token_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_ref_frame = ttk.Frame(tab_tiktok)
        tt_ref_frame.pack(fill=X, pady=5)
        ttk.Label(tt_ref_frame, text="Refresh Token:", width=18, anchor=W).pack(side=LEFT)
        tiktok_refresh_token_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_refresh_token", ""))
        ttk.Entry(tt_ref_frame, textvariable=tiktok_refresh_token_var, show="*").pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_oid_frame = ttk.Frame(tab_tiktok)
        tt_oid_frame.pack(fill=X, pady=5)
        ttk.Label(tt_oid_frame, text="Open ID / Account:", width=18, anchor=W).pack(side=LEFT)
        tiktok_open_id_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_open_id", ""))
        ttk.Entry(tt_oid_frame, textvariable=tiktok_open_id_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_uri_frame = ttk.Frame(tab_tiktok)
        tt_uri_frame.pack(fill=X, pady=5)
        ttk.Label(tt_uri_frame, text="Redirect URI (ngrok):", width=18, anchor=W).pack(side=LEFT)
        tiktok_redirect_uri_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_redirect_uri", "http://localhost:8989/"))
        ttk.Entry(tt_uri_frame, textvariable=tiktok_redirect_uri_var).pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_priv_frame = ttk.Frame(tab_tiktok)
        tt_priv_frame.pack(fill=X, pady=5)
        ttk.Label(tt_priv_frame, text="Privacy Level:", width=18, anchor=W).pack(side=LEFT)
        tiktok_privacy_level_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_privacy_level", "SELF_ONLY"))
        tt_priv_combo = ttk.Combobox(
            tt_priv_frame,
            textvariable=tiktok_privacy_level_var,
            values=["SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"],
            state="readonly"
        )
        tt_priv_combo.pack(side=LEFT, fill=X, expand=True, padx=5)

        tt_auth_frame = ttk.Frame(tab_tiktok)
        tt_auth_frame.pack(fill=X, pady=15)
        ttk.Label(tt_auth_frame, text="Authorization Status:", width=18, anchor=W).pack(side=LEFT)

        tiktok_auth_status_var = tk.StringVar()
        def check_tiktok_status():
            from pipeline.upload import get_tiktok_client
            creds, err = get_tiktok_client(self.conn)
            if creds and (creds.get("access_token") or creds.get("refresh_token")):
                tiktok_auth_status_var.set("Authorized ✅")
            else:
                tiktok_auth_status_var.set("Not Authorized ❌")

        check_tiktok_status()

        tt_status_lbl = ttk.Label(tt_auth_frame, textvariable=tiktok_auth_status_var, font=("Helvetica", 10, "bold"))
        tt_status_lbl.pack(side=LEFT, padx=5)

        def update_tt_status_style(*args):
            val = tiktok_auth_status_var.get()
            if "✅" in val:
                tt_status_lbl.configure(bootstyle="success")
            else:
                tt_status_lbl.configure(bootstyle="danger")

        tiktok_auth_status_var.trace_add("write", update_tt_status_style)
        update_tt_status_style()

        def run_tiktok_auth():
            ck = tiktok_client_key_var.get().strip()
            cs = tiktok_client_secret_var.get().strip()
            if not ck or not cs:
                messagebox.showwarning("Missing Fields", "Please enter TikTok Client Key and Client Secret first.")
                return

            tt_auth_btn.configure(state="disabled", text="Authorizing...")

            def _thread_tt_auth():
                from pipeline.upload import run_tiktok_oauth_flow
                r_uri = tiktok_redirect_uri_var.get().strip()
                success, msg = run_tiktok_oauth_flow(self.conn, ck, cs, redirect_uri=r_uri, log_callback=print)

                def done():
                    tt_auth_btn.configure(state="normal", text="Authorize TikTok")
                    if success:
                        messagebox.showinfo("Success", msg)
                        tiktok_token_var.set(db.get_setting(self.conn, "tiktok_access_token", ""))
                        tiktok_refresh_token_var.set(db.get_setting(self.conn, "tiktok_refresh_token", ""))
                        tiktok_open_id_var.set(db.get_setting(self.conn, "tiktok_open_id", ""))
                    else:
                        messagebox.showerror("Failed", msg)
                    check_tiktok_status()

                self.after(0, done)

            threading.Thread(target=_thread_tt_auth, daemon=True).start()

        def run_tiktok_refresh():
            ck = tiktok_client_key_var.get().strip()
            cs = tiktok_client_secret_var.get().strip()
            rt = tiktok_refresh_token_var.get().strip()
            if not ck or not cs or not rt:
                messagebox.showwarning("Missing Fields", "Please enter Client Key, Client Secret, and Refresh Token.")
                return

            tt_ref_btn.configure(state="disabled", text="Refreshing...")

            def _thread_tt_ref():
                from pipeline.upload import refresh_tiktok_token
                new_tok, err = refresh_tiktok_token(self.conn, ck, cs, rt)

                def done():
                    tt_ref_btn.configure(state="normal", text="Refresh Token")
                    if new_tok:
                        tiktok_token_var.set(new_tok)
                        messagebox.showinfo("Success", "TikTok Access Token successfully refreshed!")
                    else:
                        messagebox.showerror("Refresh Failed", err)
                    check_tiktok_status()

                self.after(0, done)

            threading.Thread(target=_thread_tt_ref, daemon=True).start()

        tt_ref_btn = ttk.Button(tt_auth_frame, text="Refresh Token", bootstyle="info-outline", command=run_tiktok_refresh)
        tt_ref_btn.pack(side=RIGHT, padx=5)

        tt_auth_btn = ttk.Button(tt_auth_frame, text="Authorize TikTok", bootstyle="success", command=run_tiktok_auth)
        tt_auth_btn.pack(side=RIGHT, padx=5)

        ttk.Label(
            tab_tiktok,
            text="💡 Tip: Create a Developer App on the TikTok for Developers portal and request 'video.upload' and 'video.publish' scopes for Content Posting API.",
            font=("Helvetica", 8, "italic"),
            bootstyle="secondary",
            justify=LEFT,
            wraplength=650
        ).pack(anchor=W, pady=(5, 10))

        # --- TAB: Default Templates ---
        # FB Template
        ttk.Label(tab_tpl, text="Default Facebook Post Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        fb_tpl_txt = tk.Text(tab_tpl, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        fb_tpl_txt.pack(fill=X, pady=(0, 10))
        fb_tpl_txt.insert("1.0", db.get_setting(self.conn, "fb_post_template", "{{title}}\n\n{{body}}"))

        # YT Long Title
        ttk.Label(tab_tpl, text="Default YouTube Title Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        yt_title_tpl_var = tk.StringVar(value=db.get_setting(self.conn, "yt_title_template", "{{title}}"))
        ttk.Entry(tab_tpl, textvariable=yt_title_tpl_var).pack(fill=X, pady=(0, 10))

        # YT Long Description
        ttk.Label(tab_tpl, text="Default YouTube Description Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        yt_desc_tpl_txt = tk.Text(tab_tpl, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        yt_desc_tpl_txt.pack(fill=X, pady=(0, 10))
        yt_desc_tpl_txt.insert("1.0", db.get_setting(self.conn, "yt_desc_template", "{{body}}"))

        # YT Short Title
        ttk.Label(tab_tpl, text="Default YouTube Short Title Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        short_title_tpl_var = tk.StringVar(value=db.get_setting(self.conn, "short_title_template", "{{title}} #shorts"))
        ttk.Entry(tab_tpl, textvariable=short_title_tpl_var).pack(fill=X, pady=(0, 10))

        # YT Short Description
        ttk.Label(tab_tpl, text="Default YouTube Short Description Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        short_desc_tpl_txt = tk.Text(tab_tpl, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        short_desc_tpl_txt.pack(fill=X, pady=(0, 10))
        short_desc_tpl_txt.insert("1.0", db.get_setting(self.conn, "short_desc_template", "{{body}} #shorts"))

        # TikTok Short Title
        ttk.Label(tab_tpl, text="Default TikTok Title Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        tiktok_title_tpl_var = tk.StringVar(value=db.get_setting(self.conn, "tiktok_title_template", "{{title}} #shorts #fyp #foryou"))
        ttk.Entry(tab_tpl, textvariable=tiktok_title_tpl_var).pack(fill=X, pady=(0, 10))

        # TikTok Short Description / Caption
        ttk.Label(tab_tpl, text="Default TikTok Caption / Description Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        tiktok_desc_tpl_txt = tk.Text(tab_tpl, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        tiktok_desc_tpl_txt.pack(fill=X, pady=(0, 10))
        tiktok_desc_tpl_txt.insert("1.0", db.get_setting(self.conn, "tiktok_desc_template", "{{body}}\n\n#music #sleep #relaxing #fyp #foryou"))

        # YT Short Comment
        ttk.Label(tab_tpl, text="Default YouTube Short Comment Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        short_comment_tpl_var = tk.StringVar(value=db.get_setting(self.conn, "short_comment_template", "Watch the full-length 4K video here: {{youtube-url}} 💤✨"))
        ttk.Entry(tab_tpl, textvariable=short_comment_tpl_var).pack(fill=X, pady=(0, 10))

        # Default Tags
        ttk.Label(tab_tpl, text="Default Tags (comma-separated):", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        default_tags_var = tk.StringVar(value=db.get_setting(self.conn, "default_tags", "relaxing, sleep, music, ambient, binaural"))
        ttk.Entry(tab_tpl, textvariable=default_tags_var).pack(fill=X, pady=(0, 10))

        # Gemini Quote Prompt
        ttk.Label(tab_tpl, text="Gemini Quote Prompt Template:", font=("Helvetica", 9, "bold")).pack(anchor=W, pady=(5, 2))
        gemini_quote_tpl_txt = tk.Text(tab_tpl, height=3, wrap="word", bg="#2b2b3d", fg="#e0e0e0", insertbackground="#e0e0e0")
        gemini_quote_tpl_txt.pack(fill=X, pady=(0, 10))
        gemini_quote_tpl_txt.insert("1.0", db.get_setting(self.conn, "gemini_quote_template", 'Generate a two-sentence quote for this sleep song.  Keep it simple. Generate the quote for the song titled "{{title}}".'))

        def save():
            db.set_setting(self.conn, "watch_folder", watch_var.get().strip())
            db.set_setting(self.conn, "gemini_api_key", gemini_key_var.get().strip())
            db.set_setting(self.conn, "gemini_model", gemini_model_var.get().strip())
            db.set_setting(self.conn, "fb_page_id", fb_page_id_var.get().strip())
            db.set_setting(self.conn, "fb_page_token", fb_token_var.get().strip())
            db.set_setting(self.conn, "fb_long_user_token", fb_long_user_token_var.get().strip())
            db.set_setting(self.conn, "fb_app_id", fb_app_id_var.get().strip())
            db.set_setting(self.conn, "fb_app_secret", fb_app_secret_var.get().strip())
            db.set_setting(self.conn, "yt_client_secrets", yt_secrets_var.get().strip())
            db.set_setting(self.conn, "tiktok_client_key", tiktok_client_key_var.get().strip())
            db.set_setting(self.conn, "tiktok_client_secret", tiktok_client_secret_var.get().strip())
            db.set_setting(self.conn, "tiktok_access_token", tiktok_token_var.get().strip())
            db.set_setting(self.conn, "tiktok_refresh_token", tiktok_refresh_token_var.get().strip())
            db.set_setting(self.conn, "tiktok_open_id", tiktok_open_id_var.get().strip())
            db.set_setting(self.conn, "tiktok_redirect_uri", tiktok_redirect_uri_var.get().strip())
            db.set_setting(self.conn, "tiktok_privacy_level", tiktok_privacy_level_var.get().strip() or "SELF_ONLY")
            db.set_setting(self.conn, "fb_post_template", fb_tpl_txt.get("1.0", "end-1c").strip())
            db.set_setting(self.conn, "yt_title_template", yt_title_tpl_var.get().strip())
            db.set_setting(self.conn, "yt_desc_template", yt_desc_tpl_txt.get("1.0", "end-1c").strip())
            db.set_setting(self.conn, "short_title_template", short_title_tpl_var.get().strip())
            db.set_setting(self.conn, "short_desc_template", short_desc_tpl_txt.get("1.0", "end-1c").strip())
            db.set_setting(self.conn, "tiktok_title_template", tiktok_title_tpl_var.get().strip())
            db.set_setting(self.conn, "tiktok_desc_template", tiktok_desc_tpl_txt.get("1.0", "end-1c").strip())
            db.set_setting(self.conn, "short_comment_template", short_comment_tpl_var.get().strip())
            db.set_setting(self.conn, "default_tags", default_tags_var.get().strip())
            db.set_setting(self.conn, "gemini_quote_template", gemini_quote_tpl_txt.get("1.0", "end-1c").strip())
            
            wf = watch_var.get().strip()
            self._watch_label.configure(text=f"📂 Watch: {wf or 'Not set'}")
            self._restart_watcher()
            dlg.destroy()

        ttk.Button(
            f, text="Save Settings", bootstyle="success",
            command=save,
        ).pack(anchor=E, pady=15)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    def _start_services(self):
        """Start folder watcher and pipeline worker."""
        # Reset stuck processing/deploying projects to error/interrupted state
        try:
            self.conn.execute(
                "UPDATE projects SET status = 'error', error_message = 'Processing interrupted (App restarted)' WHERE status = 'processing'"
            )
            self.conn.execute(
                "UPDATE projects SET status = 'pending_deployment', error_message = 'Upload interrupted (App restarted)' WHERE status = 'deploying'"
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
            self.after(0, lambda: self._update_detail_dynamically(project_id))

    def _periodic_refresh(self):
        """Periodically refresh the project list for processing updates."""
        try:
            self.conn.commit()
        except Exception:
            pass
        self._refresh_list()
        if self.selected_project_id:
            self._update_detail_dynamically(self.selected_project_id)
        self.after(1000, self._periodic_refresh)

    def _update_detail_dynamically(self, project_id: int):
        if self.selected_project_id != project_id:
            return
            
        try:
            self.conn.commit()
        except Exception:
            pass
            
        project = db.get_project(self.conn, project_id)
        if not project:
            return
            
        current_status = project["status"]
        saved_status = getattr(self, "_current_detail_project_status", None)
        
        # If status changed, perform a full rebuild
        if current_status != saved_status:
            self._show_project_detail(project)
            return
            
        # Update logs dynamically
        self._update_log_viewer(project_id)
        
        # Update progress bar if processing or deploying
        if current_status in ("processing", "deploying"):
            current = project.get("current_step", 0)
            total = project.get("total_steps", 13)
            if current_status == "deploying":
                total = 12
            elif current_status == "processing":
                total = 13
            pct = int(100 * current / max(total, 1))
            if pct > 100:
                pct = 100
            if hasattr(self, "_proc_status_label") and self._proc_status_label.winfo_exists():
                self._proc_status_label.configure(text=f"Progress: Step {current}/{total}")
            if hasattr(self, "_proc_progress_bar") and self._proc_progress_bar.winfo_exists():
                self._proc_progress_bar.configure(value=pct)

    def _update_log_viewer(self, project_id: int):
        if not hasattr(self, "_log_text_widget") or not self._log_text_widget or not self._log_text_widget.winfo_exists():
            return
        if getattr(self, "_current_detail_project_id", None) != project_id:
            return
            
        steps = db.get_steps(self.conn, project_id)
        active_steps = [s for s in steps if s["status"] in ("done", "running", "error") or (s.get("log_text") and s.get("log_text").strip())]
        
        log_lines = []
        for s in active_steps:
            log_lines.append(f"=== Step {s['step_num']}: {s['step_name']} ({s['status'].upper()}) ===")
            if s.get("log_text"):
                log_lines.append(s["log_text"])
            else:
                log_lines.append("[No log output yet]")
            log_lines.append("\n" + "="*60 + "\n")
            
        new_text = "\n".join(log_lines)
        
        # Avoid rewriting the text if it is identical to prevent unnecessary scrolls or flashing
        try:
            current_text = self._log_text_widget.get("1.0", "end-1c")
            if current_text == new_text:
                return
        except Exception:
            pass
            
        self._log_text_widget.configure(state="normal")
        self._log_text_widget.delete("1.0", tk.END)
        self._log_text_widget.insert("1.0", new_text)
        self._log_text_widget.configure(state="disabled")
        self._log_text_widget.see(tk.END)

    def _on_close(self):
        self._stop_player()
        self.watcher.stop()
        self.worker.stop()
        self.conn.close()
        self.destroy()
