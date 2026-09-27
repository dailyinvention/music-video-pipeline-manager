#!/usr/bin/env python3
"""
Standalone 24/7 Background Auto-Publisher for Music Video Pipeline.
Monitors SQLite database for scheduled Instagram Reels, YouTube Shorts, and Facebook posts
whose scheduled release time has arrived, and automatically publishes them.

Can be run:
  - Once (via cron/launchd): python tools/auto_publisher.py --once
  - As continuous daemon:    python tools/auto_publisher.py --daemon
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3
import argparse
from datetime import datetime, timezone

# Ensure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.upload import (
    upload_to_instagram_reel,
    get_instagram_client,
    upload_to_tiktok,
    get_tiktok_client,
    render_template,
)
from pipeline import db


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def parse_schedule_time(sched_str: str) -> datetime | None:
    if not sched_str:
        return None
    try:
        clean_iso = sched_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        # Convert naive datetime to local or compare consistently
        if dt.tzinfo is None:
            return dt
        return dt.astimezone()
    except Exception:
        return None


def is_due(sched_str: str) -> bool:
    """Returns True if the scheduled time has arrived or was in the past."""
    if not sched_str or sched_str.strip() == "":
        return False
    dt = parse_schedule_time(sched_str)
    if not dt:
        return False
    now = datetime.now() if dt.tzinfo is None else datetime.now(timezone.utc).astimezone()
    return now >= dt


def find_short_video(folder: str, pname: str) -> str | None:
    video_candidates = [
        os.path.join(folder, f"{pname} - YouTube Short.mp4"),
        os.path.join(folder, f"{pname} 4k (Video) Short.mp4"),
        os.path.join(folder, f"{pname} Short.mp4"),
    ]
    return next((p for p in video_candidates if os.path.isfile(p)), None)


def check_project_completion(conn, pid: int, pname: str):
    current_p = db.get_project(conn, pid)
    if not current_p:
        return

    fb_done = (not current_p.get("process_facebook")) or current_p.get("fb_upload_status") == "done"
    yt_done = (not current_p.get("process_youtube")) or (
        current_p.get("yt_upload_status") == "done" and current_p.get("short_upload_status") == "done"
    )
    tt_done = (not current_p.get("process_tiktok")) or current_p.get("tiktok_upload_status") == "done"
    ig_done = (not current_p.get("process_instagram")) or current_p.get("instagram_upload_status") == "done"

    if fb_done and yt_done and tt_done and ig_done:
        db.update_project(conn, pid, status="done")
        log(f"🎉 All deployment platforms finished for '{pname}'. Marked project status as 'done'.")


def check_and_publish_instagram():
    db_path = os.path.join(ROOT, "pipeline_data", "pipeline.db")
    if not os.path.isfile(db_path):
        return

    conn = db.get_connection(db_path)
    c = conn.cursor()

    # Find pending Instagram projects
    c.execute("""
        SELECT id, name, folder_path, instagram_caption_body, instagram_schedule_time,
               instagram_upload_status, process_instagram
        FROM projects
        WHERE process_instagram = 1 AND instagram_upload_status IN ('pending', 'queued')
    """)
    pending_projects = c.fetchall()

    if not pending_projects:
        conn.close()
        return

    # Check Instagram credentials
    ig_client, err = get_instagram_client(conn)
    if not ig_client or not ig_client.get("access_token") or not ig_client.get("account_id"):
        log(f"Instagram client not authorized: {err}. Skipping auto-publish check.")
        conn.close()
        return

    for proj in pending_projects:
        pid = proj["id"]
        pname = proj["name"]
        folder = proj["folder_path"]
        caption = proj["instagram_caption_body"] or f"Title: {pname}"
        sched_time = proj["instagram_schedule_time"]

        # Check if due for publishing
        if not sched_time or not is_due(sched_time):
            continue

        log(f"⏰ Scheduled time arrived for Instagram Reel '{pname}' (Target: {sched_time}). Starting upload...")

        # Locate short video file
        video_path = find_short_video(folder, pname)
        if not video_path:
            log(f"❌ Short video file for '{pname}' not found.")
            db.update_project(conn, pid, instagram_upload_status="error", error_message="Short video file not found")
            continue

        # Mark as uploading
        db.update_project(conn, pid, instagram_upload_status="uploading")

        def log_cb(msg):
            log(f"[{pname} IG] {msg}")

        media_id, upload_err = upload_to_instagram_reel(
            access_token=ig_client["access_token"],
            ig_user_id=ig_client["account_id"],
            video_path=video_path,
            caption=caption,
            log_callback=log_cb
        )

        if upload_err:
            log(f"❌ Failed to publish Instagram Reel for '{pname}': {upload_err}")
            db.update_project(
                conn, pid,
                instagram_upload_status="error",
                error_message=f"Instagram Reel publish error: {upload_err}"
            )
        else:
            log(f"✅ Successfully published Instagram Reel for '{pname}'! Media ID: {media_id}")
            db.update_project(
                conn, pid,
                instagram_upload_status="done",
                error_message=""
            )
            check_project_completion(conn, pid, pname)

    conn.close()


def check_and_publish_tiktok():
    db_path = os.path.join(ROOT, "pipeline_data", "pipeline.db")
    if not os.path.isfile(db_path):
        return

    conn = db.get_connection(db_path)
    c = conn.cursor()

    # Find pending TikTok projects
    c.execute("""
        SELECT id, name, folder_path, tiktok_title_body, tiktok_description_body, tiktok_schedule_time,
               tiktok_upload_status, process_tiktok, overlay_text, pill_badge, tags
        FROM projects
        WHERE process_tiktok = 1 AND tiktok_upload_status IN ('pending', 'queued')
    """)
    pending_projects = c.fetchall()

    if not pending_projects:
        conn.close()
        return

    # Check TikTok credentials
    tt_client, err = get_tiktok_client(conn)
    if not tt_client or not tt_client.get("access_token"):
        log(f"TikTok client not authorized: {err}. Skipping auto-publish check.")
        conn.close()
        return

    for proj in pending_projects:
        pid = proj["id"]
        pname = proj["name"]
        folder = proj["folder_path"]
        sched_time = proj["tiktok_schedule_time"]

        # Check if due for publishing
        if not sched_time or not is_due(sched_time):
            continue

        log(f"⏰ Scheduled time arrived for TikTok '{pname}' (Target: {sched_time}). Starting upload...")

        # Locate short video file
        video_path = find_short_video(folder, pname)
        if not video_path:
            log(f"❌ Short video file for '{pname}' not found.")
            db.update_project(conn, pid, tiktok_upload_status="error", error_message="Short video file not found")
            continue

        # Look up YouTube video link if available to format template
        main_yt_id = ""
        for s in db.get_steps(conn, pid):
            if s["step_num"] == 11 and s.get("output_file") and not s["output_file"].startswith("Bypassed"):
                main_yt_id = s["output_file"]
                break

        proj_dict = dict(proj)
        tt_yt_url = f"https://www.youtube.com/watch?v={main_yt_id}" if main_yt_id else ""
        tt_overlay = proj_dict.get("overlay_text", "") or ""
        pill_badge = proj_dict.get("pill_badge", "") or ""

        tt_title = render_template(proj_dict.get("tiktok_title_body", "") or "", tt_overlay, pname, yt_url=tt_yt_url, overlay_text=tt_overlay, pill_badge=pill_badge)
        tt_desc = render_template(proj_dict.get("tiktok_description_body", "") or "", tt_overlay, pname, yt_url=tt_yt_url, overlay_text=tt_overlay, pill_badge=pill_badge)

        tags_raw = proj_dict.get("tags") or ""
        tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]

        # Mark as uploading
        db.update_project(conn, pid, tiktok_upload_status="uploading")

        def log_cb(msg):
            log(f"[{pname} TT] {msg}")

        tt_privacy = db.get_setting(conn, "tiktok_privacy_level", "SELF_ONLY").strip() or "SELF_ONLY"
        pub_id, upload_err = upload_to_tiktok(
            access_token=tt_client["access_token"],
            video_path=video_path,
            title=tt_title,
            description=tt_desc,
            schedule_time_iso=sched_time,
            privacy_level=tt_privacy,
            tags=tags_list,
            log_callback=log_cb,
            conn=conn
        )

        if upload_err:
            log(f"❌ Failed to publish TikTok for '{pname}': {upload_err}")
            db.update_project(
                conn, pid,
                tiktok_upload_status="error",
                error_message=f"TikTok publish error: {upload_err}"
            )
        else:
            log(f"✅ Successfully published TikTok for '{pname}'! Publish ID: {pub_id}")
            db.update_project(
                conn, pid,
                tiktok_upload_status="done",
                error_message=""
            )
            check_project_completion(conn, pid, pname)

    conn.close()


def run_cycle():
    """Runs a single publishing check cycle for Instagram and TikTok."""
    check_and_publish_instagram()
    check_and_publish_tiktok()


def main():
    parser = argparse.ArgumentParser(description="24/7 Auto-Publisher for Instagram Reels & TikTok")
    parser.add_argument("--once", action="store_true", help="Run a single check pass and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background loop")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds (default: 300s / 5 min)")
    args = parser.parse_args()

    if args.once:
        run_cycle()
        return

    log("Starting 24/7 Auto-Publisher Daemon (Instagram & TikTok)...")
    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"Unexpected error in auto-publisher cycle: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
