#!/usr/bin/env python3
"""
Fix YouTube Shorts Descriptions & Comments Utility

1. Connects to SQLite database `pipeline_data/pipeline.db`.
2. Authenticates with YouTube Data API using stored credentials.
3. Finds all projects that have both a YouTube Short ID (step 12) and a Main YouTube Video ID (step 11).
4. Ensures YouTube Short description contains the Main YouTube Video URL.
5. Ensures YouTube Short has a top-level comment linking to the Main YouTube Video.

Usage:
    python fix_youtube_shorts.py              # Execute live fixes & comments on all YouTube Shorts
    python fix_youtube_shorts.py --dry-run    # Preview description & comment changes without modifying YouTube
    python fix_youtube_shorts.py --limit 5    # Update at most 5 Shorts
"""

import sys
import os
import json
import time
import argparse
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.upload import get_youtube_client, render_template, post_youtube_short_comment

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")


def fix_youtube_shorts(conn, dry_run=False, limit=None):
    creds, err = get_youtube_client(conn)
    if not creds:
        print(f"❌ YouTube Auth Error: {err}")
        return 0, 0

    youtube = build("youtube", "v3", credentials=creds)

    print("\n--- Scanning Database for Projects with YouTube Shorts & Main Videos ---")
    query = """
        SELECT p.id, p.name, p.overlay_text, p.short_description_body,
               l_yt.output_file AS main_yt_video_id,
               l_short.output_file AS short_yt_video_id
        FROM projects p
        JOIN process_log l_yt ON p.id = l_yt.project_id AND l_yt.step_num = 11
        JOIN process_log l_short ON p.id = l_short.project_id AND l_short.step_num = 12
        WHERE l_yt.output_file != '' AND l_yt.output_file NOT LIKE 'Bypassed%'
          AND l_short.output_file != '' AND l_short.output_file NOT LIKE 'Bypassed%'
        ORDER BY p.id DESC
    """
    cursor = conn.execute(query)
    projects = [dict(row) for row in cursor.fetchall()]

    print(f" Found {len(projects)} project(s) with both YouTube Main Video & Short Video IDs.")

    if not projects:
        return 0, 0

    template = db.get_setting(conn, "short_desc_template", "").strip()
    if not template:
        template = (
            "Relaxing music to sooth the soul and help guide you to sleep.\n\n"
            "Channel:  @music_to_sleep_to  \n"
            "Visit here to view the full-length 4k Youtube video: {{youtube-url}}\n\n"
            "© {{year}} Music To Sleep To. All Rights Reserved.\n"
            "Made with the help of Suno."
        )

    selected_projects = projects[:limit] if limit else projects
    print(f" Processing {len(selected_projects)} project(s) based on limit settings.")

    updated_count = 0
    comment_count = 0

    for proj in selected_projects:
        pid = proj["id"]
        pname = proj["name"]
        overlay_text = proj["overlay_text"] or ""
        short_id = proj["short_yt_video_id"]
        main_yt_id = proj["main_yt_video_id"]

        main_yt_url = f"https://www.youtube.com/watch?v={main_yt_id}"

        # 1. Fetch current snippet from YouTube API to preserve title, categoryId, and tags
        try:
            v_resp = youtube.videos().list(part="snippet", id=short_id).execute()
            items = v_resp.get("items", [])
            if not items:
                print(f"\n⚠️ Could not find YouTube Short [{short_id}] ({pname}) on YouTube API.")
                continue

            snippet = items[0]["snippet"]
            curr_desc = snippet.get("description", "")

            # Render new description
            body_input = proj.get("short_description_body") or template
            rendered_desc = render_template(template, body_input, pname, yt_url=main_yt_url, overlay_text=overlay_text)
            while rendered_desc.startswith('""'):
                rendered_desc = rendered_desc[1:]

            # Update description if needed
            if not (main_yt_url in curr_desc and rendered_desc.strip() == curr_desc.strip()):
                print(f"\n📌 Updating Short description for Project [{pid}] {pname}:")
                print(f"   Short ID: {short_id} | Main YT ID: {main_yt_id}")
                print(f"   YouTube Link: {main_yt_url}")

                if dry_run:
                    print("   [DRY-RUN] Would update description on YouTube API.")
                    updated_count += 1
                else:
                    snippet["description"] = rendered_desc
                    update_body = {
                        "id": short_id,
                        "snippet": snippet
                    }
                    youtube.videos().update(part="snippet", body=update_body).execute()
                    print(f"   ✅ YouTube Short [{short_id}] description updated successfully!")
                    updated_count += 1
                    time.sleep(1)

            # 2. Check & Post Comment on YouTube Short
            if dry_run:
                print(f"   [DRY-RUN] Would post comment on Short [{short_id}] with main video link: {main_yt_url}")
                comment_count += 1
            else:
                ok_c, msg_c = post_youtube_short_comment(conn, pid)
                if ok_c:
                    if "already" not in msg_c.lower():
                        print(f"   💬 {msg_c}")
                        comment_count += 1
                else:
                    print(f"   ⚠️ Comment warning for Short [{short_id}]: {msg_c}")

        except HttpError as he:
            err_msg = str(he)
            try:
                err_msg = json.loads(he.content).get("error", {}).get("message", str(he))
            except Exception:
                pass
            print(f"   ❌ YouTube API Error for Short [{short_id}]: {err_msg}")
            if "quota" in err_msg.lower():
                print("\n🛑 YouTube API daily quota limit reached (10,000 units/day). Quota resets at 12:00 AM Midnight PST.")
                break
        except Exception as e:
            print(f"   ❌ Unexpected Error for Short [{short_id}]: {e}")
            if "quota" in str(e).lower():
                print("\n🛑 YouTube API daily quota limit reached (10,000 units/day). Quota resets at 12:00 AM Midnight PST.")
                break

    return updated_count, comment_count


def main():
    parser = argparse.ArgumentParser(description="Update YouTube Shorts descriptions & post comments with Main YouTube video links.")
    parser.add_argument("--dry-run", action="store_true", help="Preview fixes without modifying YouTube videos.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of YouTube Shorts to update.")
    args = parser.parse_args()

    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        sys.exit(1)

    print("==================================================")
    print(" 🛠️  YouTube Shorts Description & Comment Sync Utility")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No live changes)")
    if args.limit:
        print(f" 🔢 LIMIT: Processing at most {args.limit} Shorts")
    print("==================================================")

    conn = db.get_connection(DB_PATH)
    try:
        desc_count, comment_count = fix_youtube_shorts(conn, dry_run=args.dry_run, limit=args.limit)

        print("\n==================================================")
        print(f" Finished! Summary:")
        print(f"  • YouTube Shorts Descriptions Updated: {desc_count}")
        print(f"  • YouTube Shorts Comments Posted: {comment_count}")
        print("==================================================")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
