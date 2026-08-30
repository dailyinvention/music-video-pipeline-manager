#!/usr/bin/env python3
"""
Enable YouTube Shorts (and Videos) Embedding Utility

Connects to YouTube Data API v3 and ensures the 'Allow embedding' setting
(status.embeddable = True) is enabled for all YouTube Shorts (or all videos).

Features:
- Scans database projects (step 12 / Shorts) or queries the YouTube Channel directly.
- Batches status checks (50 videos per API call) to conserve quota.
- Only updates videos where `embeddable` is not already enabled (saves 50 quota units per video).
- Preserves existing privacy status, schedule dates, license, and audience settings.
- Supports dry-run mode, rate limiting, and single-video targeting.

Usage:
    python enable_youtube_shorts_embedding.py                    # Process all shorts in pipeline.db
    python enable_youtube_shorts_embedding.py --dry-run          # Preview changes without modifying YouTube
    python enable_youtube_shorts_embedding.py --channel          # Scan entire YouTube channel for Shorts
    python enable_youtube_shorts_embedding.py --all-videos       # Scan all videos (both long-form and Shorts)
    python enable_youtube_shorts_embedding.py --video-id <ID>    # Process a single specific video
    python enable_youtube_shorts_embedding.py --reauth           # Trigger interactive OAuth re-authorization
"""

import sys
import os
import json
import time
import argparse
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.upload import get_youtube_client, run_youtube_oauth_flow, _is_youtube_short_duration

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")


def get_channel_uploads_playlist_id(youtube):
    """Retrieve the uploads playlist ID for the authenticated channel."""
    channels_response = youtube.channels().list(
        mine=True,
        part="contentDetails"
    ).execute()
    items = channels_response.get("items", [])
    if not items:
        return None
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


def fetch_all_channel_video_ids(youtube):
    """Retrieve all video IDs from the channel uploads playlist."""
    uploads_id = get_channel_uploads_playlist_id(youtube)
    if not uploads_id:
        print("❌ Could not find channel uploads playlist.")
        return []

    print(f"📡 Fetching all videos from channel uploads playlist ({uploads_id})...")
    video_ids = []
    next_page_token = None

    while True:
        res = youtube.playlistItems().list(
            playlistId=uploads_id,
            part="contentDetails",
            maxResults=50,
            pageToken=next_page_token
        ).execute()

        for item in res.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid and vid not in video_ids:
                video_ids.append(vid)

        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

    print(f" Found {len(video_ids)} total video(s) on YouTube channel.")
    return video_ids


def get_db_short_videos(conn):
    """Retrieve all short video records from the database."""
    query = """
        SELECT p.id AS project_id, p.name AS title, l_short.output_file AS video_id
        FROM projects p
        JOIN process_log l_short ON p.id = l_short.project_id AND l_short.step_num = 12
        WHERE l_short.output_file != '' AND l_short.output_file NOT LIKE 'Bypassed%'
        ORDER BY p.id DESC
    """
    cursor = conn.execute(query)
    return [dict(row) for row in cursor.fetchall()]


def enable_embedding(conn, dry_run=False, limit=None, scan_channel=False, all_videos=False, target_video_id=None, force=False):
    creds, err = get_youtube_client(conn)
    if not creds:
        print(f"\n⚠️ Stored YouTube credentials need re-authorization: {err}")
        print("🔄 Attempting automatic browser OAuth login...")
        if reauth_oauth(conn):
            creds, err = get_youtube_client(conn)
        if not creds:
            print(f"\n❌ YouTube Authentication Error: {err}")
            print("👉 Run `python enable_youtube_shorts_embedding.py --reauth` or configure OAuth in Settings.\n")
            return 0, 0, 0

    youtube = build("youtube", "v3", credentials=creds)

    video_items_to_check = []  # list of (video_id, title_hint, is_short)

    if target_video_id:
        print(f"\n🎯 Targeting single video ID: {target_video_id}")
        video_items_to_check.append({"video_id": target_video_id, "title": f"Video {target_video_id}", "is_short": True})
    elif scan_channel or all_videos:
        print("\n--- Scanning Channel Videos via YouTube API ---")
        channel_vids = fetch_all_channel_video_ids(youtube)
        for vid in channel_vids:
            video_items_to_check.append({"video_id": vid, "title": "", "is_short": None})
    else:
        print("\n--- Scanning Database for YouTube Shorts ---")
        db_shorts = get_db_short_videos(conn)
        print(f" Found {len(db_shorts)} YouTube Short(s) in database.")
        for item in db_shorts:
            video_items_to_check.append({"video_id": item["video_id"], "title": item["title"], "is_short": True})

    if not video_items_to_check:
        print("ℹ️ No videos found to inspect.")
        return 0, 0, 0

    total_candidates = len(video_items_to_check)
    print(f"\n🔍 Inspecting embedding status for {total_candidates} video(s)...")

    already_enabled_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0

    # Batch in groups of 50 to minimize quota consumption (1 unit per 50 videos)
    for i in range(0, len(video_items_to_check), 50):
        batch = video_items_to_check[i:i + 50]
        batch_ids = [item["video_id"] for item in batch]
        id_to_hint = {item["video_id"]: item for item in batch}

        try:
            v_resp = youtube.videos().list(
                part="status,snippet,contentDetails",
                id=",".join(batch_ids)
            ).execute()
        except HttpError as he:
            print(f"❌ YouTube API error fetching batch: {he}")
            break

        items = v_resp.get("items", [])
        for item in items:
            if limit and (updated_count >= limit):
                print(f"\n⏹️ Reached limit of {limit} updates. Stopping.")
                return already_enabled_count, updated_count, skipped_count

            vid_id = item["id"]
            snippet = item.get("snippet", {})
            status = item.get("status", {})
            content_details = item.get("contentDetails", {})

            title = snippet.get("title", id_to_hint.get(vid_id, {}).get("title", "Untitled")).strip()
            embeddable = status.get("embeddable", False)
            privacy_status = status.get("privacyStatus", "public")
            publish_at = status.get("publishAt")

            # Determine if Short when scanning whole channel without --all-videos
            duration = content_details.get("duration", "")
            is_short_duration = _is_youtube_short_duration(duration) if duration else False
            is_short_title = any(tag in title.lower() for tag in ["#short", "(short)", "#shorts"])
            is_short_record = id_to_hint.get(vid_id, {}).get("is_short")

            is_short = is_short_record is True or is_short_duration or is_short_title

            if not all_videos and not is_short and not target_video_id:
                # Skip long-form video if user didn't ask for all videos
                skipped_count += 1
                continue

            video_type = "Short" if is_short else "Video"

            if embeddable and not force:
                print(f"  ✅ [{vid_id}] ({video_type}) \"{title[:45]}\" - Embedding already allowed")
                already_enabled_count += 1
                continue

            print(f"\n  🔓 [{vid_id}] ({video_type}) \"{title[:45]}\":")
            print(f"     Current embeddable: {embeddable} | Privacy: {privacy_status}")

            if dry_run:
                print(f"     [DRY-RUN] Would enable status.embeddable = True on YouTube API.")
                updated_count += 1
                continue

            # Build status update payload preserving existing flags
            status_update = {
                "embeddable": True,
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": status.get("selfDeclaredMadeForKids", False),
            }
            if status.get("license"):
                status_update["license"] = status.get("license")
            if status.get("publicStatsViewable") is not None:
                status_update["publicStatsViewable"] = status.get("publicStatsViewable")
            if publish_at:
                status_update["publishAt"] = publish_at

            update_body = {
                "id": vid_id,
                "status": status_update
            }

            try:
                youtube.videos().update(part="status", body=update_body).execute()
                print(f"     ✨ Successfully enabled embedding!")
                updated_count += 1
                time.sleep(0.5)  # slight pause between writes
            except HttpError as he:
                err_msg = str(he)
                try:
                    err_msg = json.loads(he.content).get("error", {}).get("message", str(he))
                except Exception:
                    pass
                print(f"     ❌ Failed to update embedding: {err_msg}")
                error_count += 1
                if "quota" in err_msg.lower():
                    print("\n🛑 YouTube API daily quota limit reached. Quota resets at 12:00 AM Midnight PST.")
                    return already_enabled_count, updated_count, skipped_count
            except Exception as ex:
                print(f"     ❌ Unexpected error updating video [{vid_id}]: {ex}")
                error_count += 1

    return already_enabled_count, updated_count, skipped_count


def reauth_oauth(conn):
    """Trigger browser-based OAuth authentication to re-issue tokens."""
    client_config_json = db.get_setting(conn, "yt_client_secrets", "").strip() or db.get_setting(conn, "yt_client_config", "").strip()
    if not client_config_json:
        # Check if client_secrets.json exists in root
        secrets_path = os.path.join(ROOT, "client_secrets.json")
        if os.path.isfile(secrets_path):
            with open(secrets_path, "r", encoding="utf-8") as f:
                client_config_json = f.read().strip()
                db.set_setting(conn, "yt_client_config", client_config_json)
        else:
            print("❌ No OAuth client secrets found in database or `client_secrets.json`.")
            print("Please upload your Google Cloud OAuth Client ID JSON in the Web UI Settings.")
            return False

    print("🌐 Starting interactive YouTube OAuth flow in web browser...")
    ok, msg = run_youtube_oauth_flow(conn, client_config_json, log_callback=print)
    if ok:
        print("✅ YouTube OAuth authorization completed successfully and saved to database!")
        return True
    else:
        print(f"❌ YouTube OAuth authorization failed: {msg}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Enable 'Allow embedding' for all YouTube Shorts (and videos) on your channel."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview status without making modifications.")
    parser.add_argument("--channel", action="store_true", help="Scan the whole YouTube channel instead of just local DB.")
    parser.add_argument("--all-videos", action="store_true", help="Enable embedding for all videos (both long-form and Shorts).")
    parser.add_argument("--video-id", type=str, default=None, help="Target a specific YouTube video ID directly.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of videos to update.")
    parser.add_argument("--force", action="store_true", help="Force update even if embeddable is already True.")
    parser.add_argument("--reauth", action="store_true", help="Re-authenticate YouTube OAuth via browser.")

    args = parser.parse_args()

    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        sys.exit(1)

    conn = db.get_connection(DB_PATH)

    if args.reauth:
        reauth_oauth(conn)
        conn.close()
        return

    print("==========================================================")
    print(" 🎬 YouTube Shorts Embedding Enabler")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No live changes)")
    if args.channel:
        print(" 📡 SOURCE: YouTube Channel Uploads Playlist")
    elif args.video_id:
        print(f" 🎯 TARGET: Video ID {args.video_id}")
    else:
        print(" 🗄️  SOURCE: Local SQLite Database (`pipeline.db`)")
    if args.all_videos:
        print(" 🌟 SCOPE: All Videos (Long-form + Shorts)")
    else:
        print(" 📱 SCOPE: Shorts Only")
    if args.limit:
        print(f" 🔢 LIMIT: At most {args.limit} updates")
    print("==========================================================")

    try:
        already_enabled, updated, skipped = enable_embedding(
            conn,
            dry_run=args.dry_run,
            limit=args.limit,
            scan_channel=args.channel,
            all_videos=args.all_videos,
            target_video_id=args.video_id,
            force=args.force
        )

        print("\n==========================================================")
        print(" 📊 Summary:")
        print(f"  • Already Allowed Embedding: {already_enabled}")
        print(f"  • Updated to Allow Embedding: {updated}")
        if skipped > 0:
            print(f"  • Skipped (Long-form):       {skipped}")
        print("==========================================================")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
