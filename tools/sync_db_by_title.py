#!/usr/bin/env python3
"""
Sync SQLite Database by Matching Titles Utility

1. Reads `facebook_videos.csv` and `youtube_videos.csv`.
2. Normalizes titles to extract core project names.
3. Matches titles to projects in `pipeline_data/pipeline.db`.
4. Updates process_log and project status for Facebook (step 10), YouTube Long (step 11), and YouTube Short (step 12).

Usage:
    python sync_db_by_title.py           # Perform database update
    python sync_db_by_title.py --dry-run # Preview matching without modifying database
"""

import sys
import os
import csv
import re
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")
FB_CSV = os.path.join(ROOT, "facebook_videos.csv")
YT_CSV = os.path.join(ROOT, "youtube_videos.csv")


def normalize_title(title: str) -> str:
    """
    Cleans raw title string down to core project name.
    e.g. "Dimming The Bedside Lamp - Dim the light #relaxingmusic (Short)" -> "dimming the bedside lamp"
    """
    if not title:
        return ""

    t = title.strip()
    # Remove hashtags
    t = re.sub(r'#\S+', '', t)
    # Remove (Short) or (Video)
    t = re.sub(r'\(Short\)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\(Video\)', '', t, flags=re.IGNORECASE)
    # Split on hyphens or dashes if extra subtitle was appended
    if " - " in t:
        t = t.split(" - ")[0]
    # Remove special punctuation and extra spaces
    t = re.sub(r'[^\w\s]', '', t)
    return " ".join(t.lower().split())


def sync_db_by_title(dry_run=False):
    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        return

    if not os.path.isfile(FB_CSV):
        print(f"❌ Facebook CSV not found at: {FB_CSV}. Run export_facebook_videos.py first.")
        return

    if not os.path.isfile(YT_CSV):
        print(f"❌ YouTube CSV not found at: {YT_CSV}. Run export_youtube_videos.py first.")
        return

    conn = db.get_connection(DB_PATH)

    # 1. Load Projects from SQLite
    cursor = conn.execute("SELECT id, name FROM projects")
    db_projects = [dict(row) for row in cursor.fetchall()]

    # Map normalized title -> project dict
    norm_to_project = {}
    for p in db_projects:
        norm_name = normalize_title(p["name"])
        norm_to_project[norm_name] = p

    print(f"Loaded {len(db_projects)} project(s) from SQLite database.")

    # 2. Read Facebook CSV
    fb_matches = {}
    with open(FB_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_title = row["title"]
            fb_id = row["fb_video_id"]
            norm_t = normalize_title(raw_title)

            if norm_t in norm_to_project:
                pid = norm_to_project[norm_t]["id"]
                fb_matches[pid] = fb_id
            else:
                # Fuzzy match attempt
                for n_t, proj in norm_to_project.items():
                    if n_t in norm_t or norm_t in n_t:
                        fb_matches[proj["id"]] = fb_id
                        break

    print(f"Matched {len(fb_matches)} Facebook video(s) to database projects.")

    # 3. Read YouTube CSV (separating Main & Shorts)
    yt_main_matches = {}
    yt_short_matches = {}

    with open(YT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_title = row["title"]
            yt_id = row["yt_video_id"]
            is_short = row["is_short"].upper() == "TRUE"
            norm_t = normalize_title(raw_title)

            matched_pid = None
            if norm_t in norm_to_project:
                matched_pid = norm_to_project[norm_t]["id"]
            else:
                for n_t, proj in norm_to_project.items():
                    if n_t in norm_t or norm_t in n_t:
                        matched_pid = proj["id"]
                        break

            if matched_pid:
                if is_short:
                    yt_short_matches[matched_pid] = yt_id
                else:
                    yt_main_matches[matched_pid] = yt_id

    print(f"Matched {len(yt_main_matches)} Main YouTube video(s) and {len(yt_short_matches)} Short video(s) to database projects.")

    # 4. Perform SQLite Updates
    updated_count = 0
    for p in db_projects:
        pid = p["id"]
        pname = p["name"]

        fb_id = fb_matches.get(pid)
        yt_main_id = yt_main_matches.get(pid)
        yt_short_id = yt_short_matches.get(pid)

        if not (fb_id or yt_main_id or yt_short_id):
            continue

        print(f"\n📌 Project [{pid}] {pname}:")
        if fb_id:
            print(f"   📘 Facebook Video ID : {fb_id}")
        if yt_main_id:
            print(f"   🎬 Main YouTube ID   : {yt_main_id}")
        if yt_short_id:
            print(f"   📱 YouTube Short ID  : {yt_short_id}")

        if dry_run:
            print("   [DRY-RUN] Would update process_log and project status in SQLite.")
            updated_count += 1
        else:
            # Update Step 10 (Facebook)
            if fb_id:
                db.update_step(conn, pid, 10, status="done", output_file=fb_id, log_text=f"Synced via title match. ID: {fb_id}")
                db.update_project(conn, pid, fb_upload_status="done")

            # Update Step 11 (Main YouTube)
            if yt_main_id:
                db.update_step(conn, pid, 11, status="done", output_file=yt_main_id, log_text=f"Synced via title match. ID: {yt_main_id}")
                db.update_project(conn, pid, yt_upload_status="done")

            # Update Step 12 (YouTube Short)
            if yt_short_id:
                db.update_step(conn, pid, 12, status="done", output_file=yt_short_id, log_text=f"Synced via title match. ID: {yt_short_id}")
                db.update_project(conn, pid, short_upload_status="done")

            updated_count += 1
            print("   ✅ Updated database records!")

    conn.close()
    return updated_count


def main():
    parser = argparse.ArgumentParser(description="Sync SQLite Database by matching Facebook and YouTube video titles.")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without modifying the database.")
    args = parser.parse_args()

    print("==================================================")
    print(" 🔄 Title Matching & SQLite Sync Utility")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No live changes)")
    print("==================================================")

    count = sync_db_by_title(dry_run=args.dry_run)

    print("\n==================================================")
    print(f" Finished! Total Projects Synced: {count}")
    print("==================================================")


if __name__ == "__main__":
    main()
