#!/usr/bin/env python3
"""
Export YouTube Videos to CSV Utility

Fetches all YouTube videos (Long-form and Shorts, published and scheduled)
from the channel using search(forMine=True) and exports them to `youtube_videos.csv`.

CSV Output Columns:
    title, yt_video_id, is_short, published_at
"""

import sys
import os
import csv
from googleapiclient.discovery import build

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.upload import get_youtube_client

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")
OUTPUT_CSV = os.path.join(ROOT, "youtube_videos.csv")


def export_youtube_videos():
    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        return

    conn = db.get_connection(DB_PATH)
    creds, err = get_youtube_client(conn)
    conn.close()

    if not creds:
        print(f"❌ YouTube Auth Error: {err}")
        return

    youtube = build("youtube", "v3", credentials=creds)

    print("--- Fetching All Videos from YouTube Channel via Search API ---")

    video_ids = []
    next_page_token = None

    while True:
        res = youtube.search().list(
            forMine=True,
            type="video",
            part="snippet",
            maxResults=50,
            pageToken=next_page_token
        ).execute()

        for item in res.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in video_ids:
                video_ids.append(vid)

        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

    print(f" Found {len(video_ids)} total video IDs on YouTube Channel.")

    yt_videos = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        vids_resp = youtube.videos().list(
            part="snippet,status",
            id=",".join(batch)
        ).execute()

        for item in vids_resp.get("items", []):
            vid_id = item["id"]
            snippet = item.get("snippet", {})
            title = snippet.get("title", "Untitled").strip()
            published_at = snippet.get("publishedAt", "")

            is_short = "(short)" in title.lower() or "#shorts" in title.lower() or "#short" in title.lower()

            yt_videos.append({
                "title": title,
                "yt_video_id": vid_id,
                "is_short": "TRUE" if is_short else "FALSE",
                "published_at": published_at
            })

    print(f"\nTotal YouTube Videos Exported: {len(yt_videos)}")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "yt_video_id", "is_short", "published_at"])
        writer.writeheader()
        writer.writerows(yt_videos)

    print(f"✅ Exported YouTube videos to: {OUTPUT_CSV}")


if __name__ == "__main__":
    export_youtube_videos()
