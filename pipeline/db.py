from __future__ import annotations

"""
Database layer for the Music Video Pipeline Manager.

Uses SQLite with WAL mode for concurrent read/write access from
the GUI thread and background worker threads.
"""

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()


def get_connection(db_path: str) -> sqlite3.Connection:
    """Create a new connection with sensible defaults."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Create tables if needed and return a connection."""
    conn = get_connection(db_path)
    with _lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path     TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                status          TEXT DEFAULT 'staged',
                video_file      TEXT DEFAULT '',
                audio_file      TEXT DEFAULT '',
                overlay_text    TEXT DEFAULT '',
                loop_pick       INTEGER DEFAULT 1,
                loop_fade       REAL DEFAULT 2.0,
                loop_candidates_json TEXT DEFAULT '[]',
                font_size       INTEGER DEFAULT 90,
                fade_in_start   REAL DEFAULT 2.0,
                fade_in_end     REAL DEFAULT 4.0,
                fade_out_start  REAL DEFAULT 8.0,
                fade_out_end    REAL DEFAULT 10.0,
                proxy_path      TEXT DEFAULT '',
                current_step    INTEGER DEFAULT 0,
                total_steps     INTEGER DEFAULT 6,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message   TEXT DEFAULT '',
                negative_logo   INTEGER DEFAULT 0,
                fb_post_body    TEXT DEFAULT '',
                yt_title_body   TEXT DEFAULT '',
                yt_description_body TEXT DEFAULT '',
                short_title_body TEXT DEFAULT '',
                short_description_body TEXT DEFAULT '',
                fb_schedule_time TEXT DEFAULT '',
                yt_schedule_time TEXT DEFAULT '',
                short_schedule_time TEXT DEFAULT '',
                description_body TEXT DEFAULT '',
                fb_upload_status TEXT DEFAULT 'pending',
                yt_upload_status TEXT DEFAULT 'pending',
                short_upload_status TEXT DEFAULT 'pending',
                process_tiktok  INTEGER DEFAULT 0,
                tiktok_upload_status TEXT DEFAULT 'pending',
                tiktok_title_body TEXT DEFAULT '',
                tiktok_description_body TEXT DEFAULT '',
                tiktok_schedule_time TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS process_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                step_num    INTEGER NOT NULL,
                step_name   TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                started_at  TIMESTAMP,
                finished_at TIMESTAMP,
                output_file TEXT DEFAULT '',
                log_text    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Dynamic schema updates for new columns
        for col in ["facebook_video", "youtube_video", "facebook_audio"]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN skip_shorts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for col in ["process_facebook", "process_youtube", "process_tiktok"]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN queued_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN negative_logo INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for col in ["fb_post_body", "yt_title_body", "yt_description_body", "short_title_body", "short_description_body", "fb_schedule_time", "yt_schedule_time", "short_schedule_time", "description_body", "tiktok_title_body", "tiktok_description_body", "tiktok_schedule_time"]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        for col in ["fb_upload_status", "yt_upload_status", "short_upload_status", "tiktok_upload_status"]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT DEFAULT 'pending'")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN loop_min_dur REAL DEFAULT 10.0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN loop_max_dur REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN short_source_fb INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN tags TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        for col in ["generate_spotify_canvas", "spotify_canvas_pick"]:
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} INTEGER DEFAULT " + ("0" if col == "generate_spotify_canvas" else "1"))
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN spotify_candidates_json TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN pill_badge TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN font_style TEXT DEFAULT 'Serif'")
        except sqlite3.OperationalError:
            pass
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

def create_project(conn: sqlite3.Connection, folder_path: str,
                   name: str, video_file: str = "",
                   audio_file: str = "", facebook_video: str = "",
                   youtube_video: str = "", facebook_audio: str = "",
                   skip_shorts: int = 0, process_facebook: int = 1,
                   process_youtube: int = 1, negative_logo: int = 0,
                   process_tiktok: int = 0) -> int:
    """Insert a new project. Returns the new row id."""
    with _lock:
        cur = conn.execute(
            """INSERT OR IGNORE INTO projects
               (folder_path, name, video_file, audio_file, facebook_video, youtube_video, facebook_audio, skip_shorts, process_facebook, process_youtube, negative_logo, process_tiktok)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (folder_path, name, video_file or youtube_video, audio_file, facebook_video, youtube_video or video_file, facebook_audio, skip_shorts, process_facebook, process_youtube, negative_logo, process_tiktok),
        )
        conn.commit()
        return cur.lastrowid


def update_project(conn: sqlite3.Connection, project_id: int, **kwargs):
    """Update arbitrary columns on a project."""
    if not kwargs:
        return
    kwargs["updated_at"] = datetime.now().isoformat()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [project_id]
    with _lock:
        conn.execute(f"UPDATE projects SET {cols} WHERE id = ?", vals)
        conn.commit()


def get_project(conn: sqlite3.Connection, project_id: int) -> dict | None:
    """Fetch a single project by id."""
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return dict(row) if row else None


def get_project_by_folder(conn: sqlite3.Connection,
                          folder_path: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE folder_path = ?", (folder_path,)
    ).fetchone()
    return dict(row) if row else None


def get_all_projects(conn: sqlite3.Connection) -> list[dict]:
    """All projects, newest first."""
    rows = conn.execute(
        "SELECT * FROM projects ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_projects_by_status(conn: sqlite3.Connection,
                           status: str) -> list[dict]:
    if status == "queued":
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY COALESCE(queued_at, created_at) ASC, id ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = ? ORDER BY created_at ASC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_project(conn: sqlite3.Connection, project_id: int):
    with _lock:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Process log
# ---------------------------------------------------------------------------

def log_step(conn: sqlite3.Connection, project_id: int,
             step_num: int, step_name: str, status: str = "pending",
             output_file: str = "", log_text: str = ""):
    now = datetime.now().isoformat()
    with _lock:
        conn.execute(
            """INSERT INTO process_log
               (project_id, step_num, step_name, status, started_at,
                output_file, log_text)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project_id, step_num, step_name, status, now,
             output_file, log_text),
        )
        conn.commit()


def update_step(conn: sqlite3.Connection, project_id: int,
                step_num: int, **kwargs):
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [project_id, step_num]
    with _lock:
        conn.execute(
            f"UPDATE process_log SET {cols} "
            f"WHERE project_id = ? AND step_num = ?",
            vals,
        )
        conn.commit()


def get_steps(conn: sqlite3.Connection,
              project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM process_log WHERE project_id = ? ORDER BY step_num",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str,
                default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str):
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
