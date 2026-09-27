#!/usr/bin/env python3
"""
Test script to run the full Instagram upload & publishing flow for Flight to Celephaïs.
"""
import os
import sys
import sqlite3

# Ensure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.upload import upload_to_instagram_reel

def main():
    print("=" * 60)
    print("INSTAGRAM REEL UPLOAD TEST: Flight to Celephaïs (2m 47s)")
    print("=" * 60)

    # 1. Fetch credentials and project info from DB
    conn = sqlite3.connect(os.path.join(ROOT, "pipeline_data", "pipeline.db"))
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings WHERE key IN ('instagram_access_token', 'instagram_account_id', 'instagram_username')")
    settings = dict(c.fetchall())

    c.execute("SELECT id, name, folder_path, instagram_caption_body FROM projects WHERE name LIKE '%Celepha%'")
    project_row = c.fetchone()
    conn.close()

    token = settings.get("instagram_access_token")
    ig_user_id = settings.get("instagram_account_id")
    username = settings.get("instagram_username")

    if not token or not ig_user_id:
        print("❌ Error: Missing Instagram access token or account ID in settings.")
        sys.exit(1)

    print(f"Target Account : @{username} (ID: {ig_user_id})")

    if not project_row:
        print("❌ Error: Could not find Flight to Celephaïs project in database.")
        sys.exit(1)

    proj_id, proj_name, folder_path, caption = project_row
    video_path = os.path.join(folder_path, "Flight to Celephaïs - YouTube Short.mp4")

    if not os.path.isfile(video_path):
        print(f"❌ Error: Video file not found at {video_path}")
        sys.exit(1)

    print(f"Video File     : {video_path}")
    print(f"File Size      : {os.path.getsize(video_path) / (1024*1024):.2f} MB")
    print("-" * 60)
    print("Starting upload process...")

    def log_cb(msg):
        print(f"[*] {msg}", flush=True)

    media_id, err = upload_to_instagram_reel(
        access_token=token,
        ig_user_id=ig_user_id,
        video_path=video_path,
        caption=caption or f"Title: {proj_name}",
        schedule_time_iso="",
        log_callback=log_cb
    )

    print("-" * 60)
    if err:
        print(f"❌ Upload Failed: {err}")
        sys.exit(1)
    else:
        print(f"✅ Success! Published Instagram Reel Media ID: {media_id}")
        # Update database status
        conn = sqlite3.connect(os.path.join(ROOT, "pipeline_data", "pipeline.db"))
        c = conn.cursor()
        c.execute("UPDATE projects SET instagram_upload_status = 'done' WHERE id = ?", (proj_id,))
        conn.commit()
        conn.close()
        print("Updated database project status to 'done'.")

if __name__ == "__main__":
    main()
