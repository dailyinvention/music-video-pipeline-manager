#!/usr/bin/env python3
"""
Export Facebook Videos to CSV Utility

Fetches all videos from the configured Facebook Page via Graph API
and exports them to `facebook_videos.csv`.

CSV Output Columns:
    title, fb_video_id, created_time
"""

import sys
import os
import csv
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")
OUTPUT_CSV = os.path.join(ROOT, "facebook_videos.csv")


def clean_title_from_desc(desc: str) -> str:
    """
    Extract title if explicit title field is missing on Facebook.
    Looks for lines starting with 'Title: ...' or takes first line.
    """
    if not desc:
        return ""
    for line in desc.splitlines():
        line_s = line.strip().strip('"')
        if line_s.lower().startswith("title:"):
            title_val = line_s[6:].strip()
            # remove hashtags
            if " #" in title_val:
                title_val = title_val.split(" #")[0].strip()
            return title_val
    # Fallback to first line
    first_line = desc.strip().strip('"').splitlines()[0]
    if " #" in first_line:
        first_line = first_line.split(" #")[0].strip()
    return first_line[:100]


def export_facebook_videos():
    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        return

    conn = db.get_connection(DB_PATH)
    page_id = db.get_setting(conn, "fb_page_id", "").strip()
    page_token = db.get_setting(conn, "fb_page_token", "").strip()
    conn.close()

    if not page_id or not page_token:
        print("❌ Facebook Page ID or Page Token missing in database settings.")
        return

    print(f"--- Fetching Videos for Facebook Page ID: {page_id} ---")

    url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
    params = {
        "fields": "id,title,description,created_time",
        "limit": 100,
        "access_token": page_token
    }

    fb_videos = []

    while url:
        res = requests.get(url, params=params, timeout=30)
        res_json = res.json()

        if "error" in res_json:
            print(f"❌ Facebook API Error: {res_json['error'].get('message')}")
            break

        data = res_json.get("data", [])
        print(f" Fetched batch of {len(data)} Facebook video(s)...")

        for item in data:
            vid_id = item.get("id")
            raw_title = item.get("title", "").strip()
            desc = item.get("description", "").strip()
            created_time = item.get("created_time", "")

            title = raw_title if raw_title else clean_title_from_desc(desc)

            fb_videos.append({
                "title": title,
                "fb_video_id": vid_id,
                "created_time": created_time
            })

        paging = res_json.get("paging", {})
        url = paging.get("next")
        params = None  # Next URL already includes access token & params

    print(f"\nTotal Facebook Videos Found: {len(fb_videos)}")

    # Write to CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "fb_video_id", "created_time"])
        writer.writeheader()
        writer.writerows(fb_videos)

    print(f"✅ Exported Facebook videos to: {OUTPUT_CSV}")


if __name__ == "__main__":
    export_facebook_videos()
