#!/usr/bin/env python3
"""
YouTube Chapter Updater for Long Form Compilation Videos

This script uses the YouTube Data API v3 (authenticated via your existing credentials in pipeline_data/pipeline.db)
to automatically update a YouTube video's description with the generated tracklist / chapter bookmarks.

YouTube automatically parses these timestamps into interactive video chapters on the player bar!

Usage:
    python update_youtube_chapters.py --video-id <YOUTUBE_VIDEO_ID>
    python update_youtube_chapters.py --video-id <YOUTUBE_VIDEO_ID> --chapters-file ~/Documents/Music_To_Sleep_To_4K_Compilation_chapters.txt
    python update_youtube_chapters.py --video-id <YOUTUBE_VIDEO_ID> --prepend   # Put chapters before existing description
    python update_youtube_chapters.py --video-id <YOUTUBE_VIDEO_ID> --append    # Put chapters after existing description (default)
"""

import os
import sys
import argparse
import sqlite3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")
DEFAULT_CHAPTERS_FILE = os.path.expanduser("~/Documents/Music_To_Sleep_To_4K_Compilation_chapters.txt")


def get_youtube_client(db_path=DB_PATH):
    """Load credentials from pipeline.db settings and build YouTube service client."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Database not found at: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT key, value FROM settings WHERE key IN ('yt_credentials', 'yt_client_secrets')")
    settings = dict(cursor.fetchall())
    conn.close()

    creds_json = settings.get("yt_credentials")
    if not creds_json:
        raise ValueError("No YouTube credentials ('yt_credentials') found in pipeline.db. Run enable_youtube_shorts_embedding.py --reauth first.")

    import json
    info = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(info, scopes=["https://www.googleapis.com/auth/youtube"])
    return build("youtube", "v3", credentials=creds)


def load_chapters_text(chapters_file):
    """Load chapter bookmarks text file."""
    if not os.path.isfile(chapters_file):
        raise FileNotFoundError(f"Chapters file not found at: {chapters_file}")

    with open(chapters_file, "r", encoding="utf-8") as f:
        content = f.read().strip()
    return content


def update_video_chapters(youtube, video_id, chapters_text, position="append"):
    """
    Fetches the existing snippet for video_id, inserts/updates the chapters block,
    and calls videos().update() to apply it.
    """
    # 1. Fetch current video details
    res = youtube.videos().list(part="snippet", id=video_id).execute()
    items = res.get("items", [])
    if not items:
        print(f"❌ Video ID '{video_id}' not found on YouTube.")
        return False

    snippet = items[0]["snippet"]
    title = snippet.get("title", "")
    curr_desc = snippet.get("description", "")
    category_id = snippet.get("categoryId", "10")  # Default to Music
    tags = snippet.get("tags", [])

    print(f"📹 Found Video: \"{title}\" (ID: {video_id})")

    # 2. Check if a chapters block is already in the description
    header_marker = "⏱️ TRACKLIST & YOUTUBE CHAPTERS:"
    if header_marker in curr_desc:
        print("ℹ️ Existing chapter block detected in description. Replacing it with updated timestamps...")
        # Split out old chapters section
        parts = curr_desc.split(header_marker)
        before = parts[0].rstrip()
        # Find end of chapters section if any
        after = ""
        if "\n\n\n" in parts[1]:
            after = parts[1].split("\n\n\n", 1)[1].lstrip()
        new_desc = f"{before}\n\n{chapters_text}\n\n{after}".strip()
    else:
        if position == "prepend":
            new_desc = f"{chapters_text}\n\n{curr_desc}".strip()
        else:
            new_desc = f"{curr_desc}\n\n{chapters_text}".strip()

    # YouTube max description length is 5000 characters
    if len(new_desc) > 5000:
        print(f"⚠️ Warning: Description length ({len(new_desc)} chars) exceeds YouTube's 5,000 char limit.")
        print("Trimming sleep quotes section to fit within the 5,000 character limit...")
        # Keep only the compact timestamps part
        compact_lines = [l for l in chapters_text.split("\n") if " — \"" not in l]
        compact_chapters = "\n".join(compact_lines)
        if position == "prepend":
            new_desc = f"{compact_chapters}\n\n{curr_desc}".strip()
        else:
            new_desc = f"{curr_desc}\n\n{compact_chapters}".strip()
        new_desc = new_desc[:4995]

    # 3. Update video metadata
    update_body = {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": new_desc,
            "categoryId": category_id,
            "tags": tags
        }
    }

    youtube.videos().update(part="snippet", body=update_body).execute()
    print("✅ Successfully updated YouTube video description with interactive chapters!")
    return True


def main():
    parser = argparse.ArgumentParser(description="Automatically add or update interactive YouTube Chapters for compilation videos.")
    parser.add_argument("--video-id", type=str, required=True, help="The YouTube Video ID to update (e.g. dQw4w9WgXcQ).")
    parser.add_argument("--chapters-file", type=str, default=DEFAULT_CHAPTERS_FILE, help="Path to chapters text file (default: ~/Documents/Music_To_Sleep_To_4K_Compilation_chapters.txt).")
    parser.add_argument("--position", choices=["append", "prepend"], default="append", help="Position of chapters in description (default: append).")

    args = parser.parse_args()

    try:
        youtube = get_youtube_client()
        chapters_text = load_chapters_text(args.chapters_file)
        update_video_chapters(youtube, args.video_id, chapters_text, position=args.position)
    except Exception as ex:
        print(f"❌ Error updating YouTube chapters: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
