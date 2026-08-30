#!/usr/bin/env python3
"""
Publish Now & Sync Facebook Posts Utility

1. Connects to SQLite database `pipeline_data/pipeline.db`.
2. Authenticates with YouTube API using stored OAuth credentials.
3. Finds scheduled YouTube videos using search().list(forMine=True) sorted by scheduled publish time.
4. Filters scheduled videos to prioritize those with valid Facebook posts in SQLite.
5. If --first-only or --limit N is specified, takes the first scheduled video(s) matching valid projects.
6. Makes BOTH the Main YouTube Video and the Short YouTube Video PUBLIC immediately.
7. Renders the Facebook post template with {{youtube-url}} pointing to the main YouTube video.
8. Updates BOTH the Facebook Video object description and the Facebook Page Timeline Feed Post message via Graph API.

Usage:
    python publish_now.py              # Execute live changes for all scheduled videos
    python publish_now.py --dry-run    # Preview changes without modifying live APIs
    python publish_now.py --first-only # Process ONLY the first scheduled YouTube project & its FB post
    python publish_now.py --limit 3    # Process at most 3 items
"""

import sys
import os
import json
import argparse
import datetime
import requests
from googleapiclient.discovery import build

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.upload import get_youtube_client, render_template

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")


def find_project_by_youtube_video_id(conn, yt_vid_id: str):
    """
    Find project details from SQLite using a YouTube Video ID (matches step 11 or step 12 output_file).
    Returns dict with project info and video IDs or None.
    """
    query = """
        SELECT p.id, p.name, p.overlay_text, p.fb_post_body,
               l_fb.output_file AS fb_video_id,
               l_yt.output_file AS main_yt_video_id,
               l_short.output_file AS short_yt_video_id
        FROM projects p
        LEFT JOIN process_log l_fb ON p.id = l_fb.project_id AND l_fb.step_num = 10
        LEFT JOIN process_log l_yt ON p.id = l_yt.project_id AND l_yt.step_num = 11
        LEFT JOIN process_log l_short ON p.id = l_short.project_id AND l_short.step_num = 12
        WHERE l_yt.output_file = ? OR l_short.output_file = ?
        LIMIT 1
    """
    cursor = conn.execute(query, (yt_vid_id, yt_vid_id))
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def fetch_scheduled_youtube_videos(youtube):
    """
    Fetch all scheduled YouTube videos from the channel using search(forMine=True),
    sorted by publishAt time (earliest first).
    """
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

    if not video_ids:
        return []

    scheduled_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        vids_resp = youtube.videos().list(
            part="status,snippet",
            id=",".join(batch)
        ).execute()

        for item in vids_resp.get("items", []):
            vid_id = item["id"]
            snippet = item.get("snippet", {})
            status = item.get("status", {})
            title = snippet.get("title", "Untitled")
            privacy = status.get("privacyStatus", "")
            publish_at = status.get("publishAt", "")

            if publish_at or (privacy in ("private", "unlisted") and publish_at):
                scheduled_videos.append({
                    "id": vid_id,
                    "title": title,
                    "privacy": privacy,
                    "publish_at": publish_at
                })

    # Sort by publishAt (earliest scheduled date first)
    scheduled_videos.sort(key=lambda x: x["publish_at"] or "9999")
    return scheduled_videos


def make_youtube_video_public(youtube, vid_id: str, label: str, dry_run=False):
    """
    Ensure a specific YouTube video ID is set to PUBLIC.
    """
    if not vid_id or vid_id.startswith("Bypassed"):
        return False

    try:
        # Check current status first
        v_resp = youtube.videos().list(part="status,snippet", id=vid_id).execute()
        items = v_resp.get("items", [])
        if not items:
            print(f"   ⚠️ Could not find YouTube video [{vid_id}] on YouTube.")
            return False

        item = items[0]
        status = item.get("status", {})
        snippet = item.get("snippet", {})
        title = snippet.get("title", label)
        privacy = status.get("privacyStatus", "")

        if privacy == "public" and not status.get("publishAt"):
            print(f"   ℹ️ YouTube {label} [{vid_id}] is already PUBLIC.")
            return True

        if dry_run:
            print(f"   [DRY-RUN] Would update YouTube {label} [{vid_id}] '{title}' to PUBLIC")
            return True
        else:
            print(f"   🚀 Updating YouTube {label} [{vid_id}] '{title}' to PUBLIC...")
            youtube.videos().update(
                part="status",
                body={
                    "id": vid_id,
                    "status": {
                        "privacyStatus": "public"
                    }
                }
            ).execute()
            print(f"   ✅ YouTube {label} [{vid_id}] is now PUBLIC!")
            return True
    except Exception as e:
        print(f"   ❌ YouTube API Error updating {label} [{vid_id}]: {e}")
        return False


def update_facebook_post_for_project(conn, project, page_id, page_token, template, dry_run=False):
    """
    Renders and updates BOTH the Video description and Feed Post message on Facebook for a specific project.
    """
    pid = project["id"]
    pname = project["name"]
    overlay_text = project["overlay_text"] or ""
    fb_body = project["fb_post_body"] or ""
    fb_vid_id = project["fb_video_id"]
    yt_vid_id = project["main_yt_video_id"]

    if not fb_vid_id or fb_vid_id.startswith("Bypassed"):
        print(f"   ℹ️ Project [{pid}] {pname} has no valid Facebook video ID to update.")
        return False

    if not yt_vid_id or yt_vid_id.startswith("Bypassed"):
        print(f"   ⚠️ Project [{pid}] {pname} has no valid main YouTube video ID.")
        return False

    yt_url = f"https://www.youtube.com/watch?v={yt_vid_id}"

    # Render template
    rendered_desc = render_template(template, fb_body, pname)
    rendered_desc = rendered_desc.replace("{{youtube-url}}", yt_url)
    rendered_desc = rendered_desc.replace("{{overlay_text}}", overlay_text)
    while rendered_desc.startswith('""'):
        rendered_desc = rendered_desc[1:]

    print(f"\n📘 Syncing Facebook Post for Project [{pid}] {pname}:")
    print(f"   FB Video ID: {fb_vid_id} | Main YT Video ID: {yt_vid_id}")
    print(f"   YouTube Link: {yt_url}")

    if dry_run:
        print("   [DRY-RUN] Generated Description:")
        print("   " + "\n   ".join(rendered_desc.splitlines()[:5]))
        return True
    else:
        print("   Updating Facebook Video & Timeline Feed Post text via Graph API...")
        url_vid = f"https://graph.facebook.com/v19.0/{fb_vid_id}"
        payload_vid = {
            "access_token": page_token,
            "description": rendered_desc
        }
        res_vid = requests.post(url_vid, data=payload_vid, timeout=30).json()

        post_updated = False
        try:
            get_resp = requests.get(url_vid, params={"fields": "post_id", "access_token": page_token}, timeout=15).json()
            post_id = get_resp.get("post_id")
            if post_id:
                full_post_id = f"{page_id}_{post_id}" if "_" not in str(post_id) and page_id else str(post_id)
                url_post = f"https://graph.facebook.com/v19.0/{full_post_id}"
                payload_post = {
                    "access_token": page_token,
                    "message": rendered_desc
                }
                res_post = requests.post(url_post, data=payload_post, timeout=30).json()
                if res_post.get("success", False) or "id" in res_post:
                    post_updated = True
        except Exception:
            pass

        if res_vid.get("success", False) or "id" in res_vid or post_updated:
            print(f"   ✅ Facebook Post & Video [{fb_vid_id}] updated successfully!")
            return True
        else:
            err_msg = res_vid.get("error", {}).get("message", "Unknown FB error")
            print(f"   ❌ Facebook API Error for [{fb_vid_id}]: {err_msg}")
            return False


def process_sync(conn, dry_run=False, limit=None, require_fb_match=True):
    """
    Main sync logic:
    1. Authenticate YouTube.
    2. Get all scheduled videos on YouTube (sorted by scheduled date).
    3. Match each video to its project in SQLite.
    4. Deduplicate by project so each project's Main Video and Short Video are processed together.
    5. Update BOTH Main Video and Short Video to PUBLIC on YouTube.
    6. Update matching Facebook post and feed message with the Main YouTube URL.
    """
    creds, err = get_youtube_client(conn)
    if not creds:
        print(f"❌ YouTube Auth Error: {err}")
        return 0, 0

    youtube = build("youtube", "v3", credentials=creds)

    print("\n--- [Step 1] Fetching Scheduled Videos from YouTube ---")
    scheduled_videos = fetch_scheduled_youtube_videos(youtube)
    print(f" Found {len(scheduled_videos)} total scheduled video(s) on YouTube.")

    if not scheduled_videos:
        print("ℹ️ No scheduled videos found on YouTube.")
        return 0, 0

    projects_to_process = []
    seen_project_ids = set()

    for vid in scheduled_videos:
        project = find_project_by_youtube_video_id(conn, vid["id"])
        if project:
            pid = project["id"]
            if pid not in seen_project_ids:
                seen_project_ids.add(pid)
                projects_to_process.append(project)

    if require_fb_match:
        valid_fb_projects = [
            p for p in projects_to_process
            if p.get("fb_video_id") and not p["fb_video_id"].startswith("Bypassed")
        ]
        if valid_fb_projects:
            print(f" Filtered to {len(valid_fb_projects)} project(s) with matching Facebook posts in SQLite.")
            target_projects = valid_fb_projects
        else:
            print(" ⚠️ No scheduled videos have matching Facebook posts in SQLite. Processing all matched projects.")
            target_projects = projects_to_process
    else:
        target_projects = projects_to_process

    selected_projects = target_projects[:limit] if limit else target_projects
    print(f" Processing {len(selected_projects)} project(s) based on limit settings.")

    page_id = db.get_setting(conn, "fb_page_id", "").strip()
    page_token = db.get_setting(conn, "fb_page_token", "").strip()
    template = db.get_setting(conn, "fb_post_template", "").strip()
    if not template:
        template = "{{title}}\n\n{{body}}\n\nWatch on YouTube: {{youtube-url}}"

    yt_published_count = 0
    fb_updated_count = 0

    for project in selected_projects:
        pid = project["id"]
        pname = project["name"]
        main_yt_id = project.get("main_yt_video_id")
        short_yt_id = project.get("short_yt_video_id")

        print(f"\n📌 Processing Project [{pid}] {pname}:")

        # 1. Update Main YouTube Video to Public
        if main_yt_id:
            if make_youtube_video_public(youtube, main_yt_id, "Main Video", dry_run=dry_run):
                yt_published_count += 1

        # 2. Update Short YouTube Video to Public
        if short_yt_id:
            if make_youtube_video_public(youtube, short_yt_id, "Short Video", dry_run=dry_run):
                yt_published_count += 1

        # 3. Update Facebook Post (both Video description & Feed Post message)
        if page_id and page_token:
            if update_facebook_post_for_project(conn, project, page_id, page_token, template, dry_run=dry_run):
                fb_updated_count += 1
        else:
            print("   ⚠️ Facebook credentials missing in database settings. Skipping Facebook sync.")

        # 4. Update YouTube Short description & post comment with Main YouTube Video link
        if short_yt_id and main_yt_id:
            if dry_run:
                print(f"   [DRY-RUN] Would update YouTube Short [{short_yt_id}] description & post pinned comment with Main YouTube link (https://www.youtube.com/watch?v={main_yt_id})")
            else:
                from pipeline.upload import sync_youtube_short_with_main_url, post_youtube_short_comment
                try:
                    ok_short, msg_short = sync_youtube_short_with_main_url(conn, pid, log_callback=print)
                    if ok_short:
                        print(f"   ✅ {msg_short}")
                    else:
                        print(f"   ⚠️ Could not sync YouTube Short: {msg_short}")
                except Exception as sync_err:
                    print(f"   ⚠️ Error syncing YouTube Short: {sync_err}")

                try:
                    ok_comment, msg_comment = post_youtube_short_comment(conn, pid, log_callback=print)
                    if ok_comment:
                        print(f"   💬 {msg_comment}")
                    else:
                        print(f"   ⚠️ Could not post YouTube Short comment: {msg_comment}")
                except Exception as c_err:
                    print(f"   ⚠️ Error posting YouTube Short comment: {c_err}")

    return yt_published_count, fb_updated_count


def main():
    parser = argparse.ArgumentParser(description="Publish scheduled YouTube videos & sync matching Facebook post descriptions with YouTube links.")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without modifying YouTube or Facebook API state.")
    parser.add_argument("--first-only", action="store_true", help="Process ONLY the first scheduled YouTube project (Main + Short) & its FB post.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the maximum number of scheduled projects to process (e.g. --limit 1).")
    parser.add_argument("--include-no-fb", action="store_true", help="Include scheduled videos even if they do not have a Facebook post in SQLite.")
    args = parser.parse_args()

    limit = 1 if args.first_only else args.limit
    require_fb_match = not args.include_no_fb

    if not os.path.isfile(DB_PATH):
        print(f"❌ Database file not found at: {DB_PATH}")
        sys.exit(1)

    print("==================================================")
    print(" 🚀 YouTube & Facebook Sync Utility")
    if args.dry_run:
        print(" ⚠️  RUNNING IN DRY-RUN MODE (No live changes)")
    if limit:
        print(f" 🔢 LIMIT: Processing at most {limit} project(s)")
    if require_fb_match:
        print(" 🎯 FILTER: Matching only scheduled projects with valid Facebook posts in SQLite")
    print("==================================================")

    conn = db.get_connection(DB_PATH)
    try:
        yt_count, fb_count = process_sync(conn, dry_run=args.dry_run, limit=limit, require_fb_match=require_fb_match)

        print("\n==================================================")
        print(f" Finished! Summary:")
        print(f"  • YouTube Videos Made Public: {yt_count}")
        print(f"  • Matching Facebook Posts Updated with YT Link: {fb_count}")
        print("==================================================")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
