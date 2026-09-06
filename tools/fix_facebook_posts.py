#!/usr/bin/env python3
"""
Fix Facebook Posts Double Quotes & Sync Feed Post Messages Utility

1. Updates `fb_post_template` in SQLite database to remove extra outer double quotes around {{body}}.
2. Re-renders clean post text replacing {{body}} with only the extracted quote.
3. Updates BOTH the Facebook Video Object description and the Timeline Feed Post message via Graph API.

Usage:
    python fix_facebook_posts.py           # Execute live fixes on all Facebook posts
    python fix_facebook_posts.py --dry-run # Preview description fixes without modifying Facebook
"""

import sys
import os
import time
import argparse
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.upload import render_template, sync_facebook_post_with_youtube_url

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")


def fix_db_template(conn):
    """
    Clean up fb_post_template in settings database if it contains "{{body}}" with extra outer quotes.
    """
    tpl = db.get_setting(conn, "fb_post_template", "").strip()
    if tpl.startswith('"{{body}}"'):
        new_tpl = tpl.replace('"{{body}}"', '{{body}}', 1)
        db.set_setting(conn, "fb_post_template", new_tpl)
        print("✅ Fixed database setting 'fb_post_template' (removed extra quotes around {{body}}).")


def fix_facebook_posts(conn, dry_run=False):
    page_id = db.get_setting(conn, "fb_page_id", "").strip()
    page_token = db.get_setting(conn, "fb_page_token", "").strip()

    if not page_id or not page_token:
        print("❌ Facebook Page ID or Page Access Token is missing in database settings.")
        return 0

    print("\n--- Scanning Database Projects for Facebook Video IDs ---")
    query = """
        SELECT p.id, p.name, l_fb.output_file AS fb_video_id
        FROM projects p
        JOIN process_log l_fb ON p.id = l_fb.project_id AND l_fb.step_num = 10
        WHERE l_fb.status = 'done' AND l_fb.output_file != '' AND l_fb.output_file NOT LIKE 'Bypassed%'
        ORDER BY p.id DESC
    """
    cursor = conn.execute(query)
    projects = [dict(row) for row in cursor.fetchall()]

    print(f" Found {len(projects)} Facebook video post(s) in local database.")

    fixed_count = 0

    for proj in projects:
        pid = proj["id"]
        pname = proj["name"]
        fb_vid_id = proj["fb_video_id"]

        print(f"\n📌 Re-rendering and updating FB Video [{fb_vid_id}] ({pname}):")

        if dry_run:
            print(f"   [DRY-RUN] Would re-render and update Video description & Timeline Feed Post message for Project [{pid}]")
            fixed_count += 1
        else:
            success, msg = sync_facebook_post_with_youtube_url(conn, pid, log_callback=print)
            if success:
                print(f"   ✅ {msg}")
                fixed_count += 1
            else:
                print(f"   ❌ Error updating [{fb_vid_id}]: {msg}")

            # Rate limit pacing
            time.sleep(3)

    return fixed_count


def main():
    parser = argparse.ArgumentParser(description="Fix double quotes & clean post descriptions on Facebook.")
    parser.add_argument("--dry-run", action="store_true", help="Preview fixes without modifying Facebook posts.")
    args = parser.parse_args()

    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        sys.exit(1)

    print("==================================================")
    print(" 🛠️  Facebook Post Clean & Sync Utility")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No live changes)")
    print("==================================================")

    conn = db.get_connection(DB_PATH)
    try:
        fix_db_template(conn)
        count = fix_facebook_posts(conn, dry_run=args.dry_run)

        print("\n==================================================")
        print(f" Finished! Summary:")
        print(f"  • Facebook Posts Updated & Cleaned: {count}")
        print("==================================================")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
