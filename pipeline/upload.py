from __future__ import annotations
"""
Upload backend for Facebook Graph API and YouTube Data API v3.
Implements resumable uploads for large video files and template parsing.
"""

import os
import json
import time
import datetime
import requests
from pathlib import Path

# Google API libraries
try:
    import google.oauth2.credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    pass

from pipeline import db

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]


# ---------------------------------------------------------------------------
# Simple Template Renderer (Handlebars-style)
# ---------------------------------------------------------------------------

def extract_quote(body_text: str) -> str:
    """Extracts only the quote/caption from a full body string if it contains extra metadata."""
    if not body_text:
        return ""
    lines = []
    for line in body_text.splitlines():
        line_s = line.strip().lower()
        if (line_s.startswith("title:") or 
            line_s.startswith("©") or 
            line_s.startswith("made with") or
            line_s.startswith("view the full youtube") or
            line_s.startswith("watch on youtube")):
            break
        lines.append(line)
    quote = "\n".join(lines).strip()
    while quote.startswith('"'):
        quote = quote[1:]
    while quote.endswith('"'):
        quote = quote[:-1]
    return quote.strip()


def render_template(template_str: str, body_text: str, project_title: str, yt_url: str = "", overlay_text: str = "", pill_badge: str = "", **kwargs) -> str:
    """
    Renders template string replacing placeholders with context variables:
    {{body}}, {{quote}}, {{title}}, {{pill_badge}}, {{badge}}, {{youtube-url}}, {{youtube_url}}, {{overlay_text}}, {{year}}, {{month}}, {{day}}, {{date}}
    """
    now = datetime.datetime.now()
    clean_q = extract_quote(body_text)

    context = {
        "body": clean_q or (body_text or ""),
        "quote": clean_q or (body_text or ""),
        "title": project_title or "",
        "overlay_text": overlay_text or "",
        "pill_badge": pill_badge or "",
        "pill-badge": pill_badge or "",
        "badge": pill_badge or "",
        "year": str(now.year),
        "month": now.strftime("%B"), # Month name (e.g. "May")
        "day": str(now.day),
        "date": now.strftime("%Y-%m-%d"),
    }

    if yt_url:
        context["youtube-url"] = yt_url
        context["youtube_url"] = yt_url
    else:
        # Preserve placeholder if URL is not yet available
        context["youtube-url"] = "{{youtube-url}}"
        context["youtube_url"] = "{{youtube-url}}"
    
    rendered = template_str or "{{body}}"
    for key, val in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", val)

    while rendered.startswith('""'):
        rendered = rendered[1:]

    return rendered


# ---------------------------------------------------------------------------
# YouTube OAuth & API Client Setup
# ---------------------------------------------------------------------------

def get_youtube_client(conn) -> tuple[google.oauth2.credentials.Credentials | None, str]:
    """
    Retrieve stored YouTube credentials from settings database, refresh if expired,
    and return the Credentials object.
    """
    creds_json = db.get_setting(conn, "yt_credentials", "").strip()
    if not creds_json:
        return None, "YouTube accounts is not authorized. Please configure OAuth in Settings."
        
    try:
        creds_dict = json.loads(creds_json)
        
        # Check if the stored credentials have all required scopes
        creds_scopes = creds_dict.get("scopes", [])
        has_full_yt = "https://www.googleapis.com/auth/youtube" in creds_scopes
        if not has_full_yt:
            missing_scopes = [s for s in SCOPES if s not in creds_scopes]
            if missing_scopes:
                return None, f"YouTube credentials have insufficient scopes (missing: {', '.join(missing_scopes)}). Please re-authorize YouTube in Settings."
            
        creds = google.oauth2.credentials.Credentials.from_authorized_user_info(creds_dict, SCOPES)
        
        # Refresh if expired
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            # Save refreshed credentials back to DB
            db.set_setting(conn, "yt_credentials", creds.to_json())
            
        return creds, ""
    except Exception as e:
        return None, f"Failed to restore YouTube credentials: {e}"


def run_youtube_oauth_flow(conn, client_config_json: str, log_callback=None) -> tuple[bool, str]:
    """
    Run the OAuth flow inside a local server context and save credentials.
    client_config_json is the string contents of client_secrets.json.
    """
    try:
        client_config = json.loads(client_config_json)
        # Ensure it has the correct layout
        if "web" not in client_config and "installed" not in client_config:
            return False, "Invalid OAuth Client secrets JSON structure. Must contain 'installed' or 'web' root key."
            
        flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
        if log_callback:
            log_callback("Opening web browser for authentication...")
            
        # Run local server
        creds = flow.run_local_server(
            port=0,
            authorization_prompt_message="Please visit this URL to authorize this application: {url}",
            success_message="Authorization complete! You can close this window.",
            prompt="consent"
        )
        
        # Save credentials to database
        db.set_setting(conn, "yt_credentials", creds.to_json())
        return True, "YouTube channel successfully authorized!"
    except Exception as e:
        return False, f"OAuth Flow failed: {e}"


# ---------------------------------------------------------------------------
# YouTube Resumable Video Upload
# ---------------------------------------------------------------------------

def upload_to_youtube(
    creds,
    video_path: str,
    title: str,
    description: str,
    schedule_time_iso: str = "",  # "YYYY-MM-DDThh:mm:ss.sZ"
    privacy_status: str = "public",
    tags: list[str] | None = None,
    log_callback=None
) -> tuple[str, str]:
    """
    Perform a resumable chunked video upload to YouTube using the Data API v3.
    """
    if not os.path.isfile(video_path):
        return "", f"Video file not found: {video_path}"
        
    try:
        youtube = build("youtube", "v3", credentials=creds)
        
        # Prepare metadata
        snippet = {
            "title": title[:100],  # Max 100 characters
            "description": description[:5000],  # Max 5000 characters
            "categoryId": "10",  # Music category
        }
        if tags:
            snippet["tags"] = [t.strip() for t in tags if t.strip()][:30]
        
        status = {
            "privacyStatus": "private",  # Must be private to support schedule_time
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        }
        
        # Handle scheduling (e.g. "2026-06-01T12:00:00Z" or "2026-06-01T12:00:00-04:00")
        if schedule_time_iso:
            if not schedule_time_iso.endswith("Z") and "+" not in schedule_time_iso and "-" not in schedule_time_iso[10:]:
                try:
                    # Parse local naive ISO and localize it to the current system timezone
                    dt = datetime.datetime.fromisoformat(schedule_time_iso)
                    local_tz = datetime.datetime.now().astimezone().tzinfo
                    dt_aware = dt.replace(tzinfo=local_tz)
                    schedule_time_iso = dt_aware.isoformat()
                except Exception as ex:
                    if log_callback:
                        log_callback(f"Warning: Failed to localize schedule time for YouTube: {ex}")
            status["publishAt"] = schedule_time_iso
            status["privacyStatus"] = "private"
        else:
            status["privacyStatus"] = privacy_status
            
        body = {
            "snippet": snippet,
            "status": status,
        }
        
        # 5MB chunk sizes (must be multiples of 256KB)
        media = MediaFileUpload(video_path, chunksize=1024 * 1024 * 5, resumable=True)
        
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )
        
        if log_callback:
            log_callback(f"Starting YouTube upload of {os.path.basename(video_path)} ({os.path.getsize(video_path) / (1024 * 1024):.1f} MB)...")
            
        response = None
        retry_count = 0
        max_retries = 10
        while response is None:
            try:
                status, response = request.next_chunk()
                retry_count = 0  # reset on successful progress
                if status:
                    progress = int(status.progress() * 100)
                    if log_callback:
                        log_callback(f"Uploading to YouTube: {progress}% complete...")
            except Exception as e:
                retry_count += 1
                if retry_count > max_retries:
                    raise e
                sleep_time = min(2 ** retry_count, 60)
                if log_callback:
                    log_callback(f"Transient error during upload ({e}). Retrying in {sleep_time}s (attempt {retry_count}/{max_retries})...")
                time.sleep(sleep_time)
                    
        video_id = response.get("id", "")
        if log_callback:
            log_callback(f"YouTube upload complete! Video ID: {video_id}")
            
        return video_id, ""
    except HttpError as he:
        try:
            error_details = json.loads(he.content).get("error", {}).get("message", str(he))
        except Exception:
            error_details = str(he)
        return "", f"YouTube API Error: {error_details}"
    except Exception as e:
        return "", f"YouTube Upload failed: {e}"


def update_youtube_video_status(
    creds,
    video_id: str,
    schedule_time_iso: str = "",
    privacy_status: str = "public",
    log_callback=None
) -> tuple[bool, str]:
    """Update the status of an existing YouTube video (e.g. set to Scheduled)."""
    try:
        youtube = build("youtube", "v3", credentials=creds)
        
        status = {
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        }
        if schedule_time_iso:
            if not schedule_time_iso.endswith("Z") and "+" not in schedule_time_iso and "-" not in schedule_time_iso[10:]:
                try:
                    dt = datetime.datetime.fromisoformat(schedule_time_iso)
                    local_tz = datetime.datetime.now().astimezone().tzinfo
                    dt_aware = dt.replace(tzinfo=local_tz)
                    schedule_time_iso = dt_aware.isoformat()
                except Exception:
                    pass
            status["publishAt"] = schedule_time_iso
            status["privacyStatus"] = "private"
        else:
            status["privacyStatus"] = privacy_status
            
        body = {
            "id": video_id,
            "status": status
        }
        
        if log_callback:
            log_callback(f"Updating status for YouTube video {video_id}...")
            
        youtube.videos().update(
            part="status",
            body=body
        ).execute()
        
        if log_callback:
            log_callback(f"Video {video_id} status updated successfully!")
            
        return True, ""
    except HttpError as he:
        try:
            error_details = json.loads(he.content).get("error", {}).get("message", str(he))
        except Exception:
            error_details = str(he)
        return False, f"YouTube API Error: {error_details}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Facebook Page Resumable Video Upload
# ---------------------------------------------------------------------------

def upload_to_facebook_page(
    page_id: str,
    page_access_token: str,
    video_path: str,
    title: str,
    description: str,
    schedule_time_iso: str = "",  # "YYYY-MM-DDThh:mm:ss.sZ"
    tags: list[str] | None = None,
    log_callback=None
) -> tuple[str, str]:
    """
    Upload a video to a Facebook Page using FB Graph API's resumable protocol.
    Does not depend on deprecated facebook-sdk wrappers.
    """
    if not page_id or not page_access_token:
        return "", "Facebook Page ID and Page Access Token are required."
        
    if not os.path.isfile(video_path):
        return "", f"Video file not found: {video_path}"
        
    file_size = os.path.getsize(video_path)
    base_url = f"https://graph.facebook.com/v26.0/{page_id}/videos"
    
    try:
        # Phase 1: Start Session
        if log_callback:
            log_callback("Initializing Facebook upload session...")
            
        start_payload = {
            "upload_phase": "start",
            "access_token": page_access_token,
            "file_size": file_size,
        }
        res = requests.post(base_url, data=start_payload, timeout=30)
        res_json = res.json()
        
        if "error" in res_json:
            return "", f"FB Init Error: {res_json['error'].get('message')}"
            
        upload_session_id = res_json.get("upload_session_id")
        video_id = res_json.get("video_id")
        if not upload_session_id:
            return "", "Facebook API did not return an upload session ID."
            
        # Phase 2: Transfer Chunks
        # Default FB chunk size is 1MB - 10MB
        chunk_size = 1024 * 1024 * 4  # 4MB chunks
        start_offset = int(res_json.get("start_offset", 0))
        end_offset = int(res_json.get("end_offset", 0))
        
        if log_callback:
            log_callback(f"Facebook upload session started. Transferring chunks ({file_size / (1024*1024):.1f} MB)...")
            
        with open(video_path, "rb") as f:
            while start_offset < file_size:
                f.seek(start_offset)
                chunk_data = f.read(chunk_size)
                
                transfer_payload = {
                    "upload_phase": "transfer",
                    "access_token": page_access_token,
                    "upload_session_id": upload_session_id,
                    "start_offset": start_offset,
                }
                
                files = {
                    "video_file_chunk": chunk_data
                }
                
                # Retry chunk transfer on network glitch
                for attempt in range(3):
                    try:
                        res = requests.post(base_url, data=transfer_payload, files=files, timeout=90)
                        res_json = res.json()
                        break
                    except requests.exceptions.RequestException as re_err:
                        if attempt == 2:
                            raise re_err
                        time.sleep(2)
                        
                if "error" in res_json:
                    return "", f"FB Chunk Upload Error: {res_json['error'].get('message')}"
                    
                start_offset = int(res_json.get("start_offset", start_offset))
                end_offset = int(res_json.get("end_offset", end_offset))
                
                progress = int((start_offset / file_size) * 100)
                if log_callback:
                    log_callback(f"Uploading to Facebook: {progress}% complete...")
                    
        # Phase 3: Finish Session
        if log_callback:
            log_callback("Finishing Facebook upload and updating metadata...")
            
        finish_payload = {
            "upload_phase": "finish",
            "access_token": page_access_token,
            "upload_session_id": upload_session_id,
            "title": title[:200],  # FB title limit
            "description": description,
            "custom_labels": json.dumps([t.strip() for t in tags if t.strip()]) if tags else json.dumps(["relaxing", "sleep", "music", "ambient", "binaural"]),
            "allow_remix": "false",
        }
        
        # Scheduling
        if schedule_time_iso:
            try:
                # Convert ISO 8601 to UNIX timestamp
                # Supports formats like YYYY-MM-DDThh:mm:ss
                clean_time = schedule_time_iso.rstrip("Z").split(".")[0]
                dt = datetime.datetime.fromisoformat(clean_time)
                timestamp = int(dt.timestamp())
                
                finish_payload["published"] = "false"
                finish_payload["scheduled_publish_time"] = timestamp
                
                # FB requires scheduling to be between 10 mins and 30 days in the future
                time_diff = timestamp - int(time.time())
                if time_diff < 600:
                    # Too close, warn and fall back to immediate or min 11 minutes
                    if log_callback:
                        log_callback("Warning: schedule time is less than 10 minutes in the future. Adjusting to 12 minutes in the future to satisfy FB rules.")
                    finish_payload["scheduled_publish_time"] = int(time.time()) + 720
                elif time_diff > 30 * 24 * 3600:
                    return "", "Facebook schedule time cannot exceed 30 days into the future."
            except Exception as e:
                return "", f"Failed to parse schedule time for Facebook: {e}"
        else:
            finish_payload["published"] = "true"
            
        res = requests.post(base_url, data=finish_payload, timeout=30)
        res_json = res.json()
        
        if "error" in res_json:
            return "", f"FB Finish Error: {res_json['error'].get('message')}"
            
        if not res_json.get("success", False):
            return "", "Facebook API did not return success for session finish."
            
        if log_callback:
            log_callback(f"Facebook upload complete! Page Post Video ID: {video_id}")
            
        return video_id, ""
    except Exception as e:
        return "", f"Facebook Upload failed: {e}"


def get_last_youtube_scheduled_dates(creds) -> tuple[datetime.datetime | None, datetime.datetime | None, str]:
    """
    Fetch the most recent scheduled publish times (or fallback upload times) for YouTube long videos and Shorts.
    Shorts are identified by duration <= 60 seconds or title/description keywords.
    Returns (last_long_dt, last_short_dt, error_message) in local time.
    """
    try:
        youtube = build("youtube", "v3", credentials=creds)
        
        # 1. Fetch channel uploads playlist ID
        channels_response = youtube.channels().list(
            mine=True,
            part="contentDetails"
        ).execute()
        
        if not channels_response.get("items"):
            return None, None, "No channels found for the authenticated user."
            
        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        
        # 2. Fetch video IDs from the uploads playlist (page up to 4 times / 200 items to locate shorts)
        video_ids = []
        next_page_token = None
        for _ in range(4):
            playlist_response = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="contentDetails",
                maxResults=50,
                pageToken=next_page_token
            ).execute()
            
            for item in playlist_response.get("items", []):
                vid = item.get("contentDetails", {}).get("videoId")
                if vid and vid not in video_ids:
                    video_ids.append(vid)
                    
            next_page_token = playlist_response.get("nextPageToken")
            if not next_page_token:
                break
        
        long_dates = []
        short_dates = []
        
        if video_ids:
            # 3. Fetch detailed status and contentDetails in batches of 50
            for i in range(0, len(video_ids), 50):
                batch_ids = video_ids[i:i+50]
                response = youtube.videos().list(
                    part="status,snippet,contentDetails",
                    id=",".join(batch_ids)
                ).execute()
        
                for item in response.get("items", []):
                    publish_at = item.get("status", {}).get("publishAt")
                    published_at = item.get("snippet", {}).get("publishedAt")
                    
                    date_str = publish_at or published_at
                    if not date_str:
                        continue
                        
                    dt_utc = datetime.datetime.fromisoformat(date_str.rstrip("Z")).replace(tzinfo=datetime.timezone.utc)
                    dt_local = dt_utc.astimezone().replace(tzinfo=None)
        
                    # Determine if Short: scheduled weekday is the most reliable check.
                    is_short = False
                    if publish_at:
                        weekday = dt_local.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
                        if weekday in (0, 3):  # Monday or Thursday
                            is_short = True
                        elif weekday in (1, 4):  # Tuesday or Friday
                            is_short = False
                        else:
                            # Fallback if scheduled on an off-day
                            duration = item.get("contentDetails", {}).get("duration", "")
                            title = item.get("snippet", {}).get("title", "").lower()
                            description = item.get("snippet", {}).get("description", "").lower()
                            is_short = (
                                _is_youtube_short_duration(duration) or
                                "short" in title or
                                "short" in description
                            )
                    else:
                        # For already published videos, use duration and broad keyword checks
                        duration = item.get("contentDetails", {}).get("duration", "")
                        title = item.get("snippet", {}).get("title", "").lower()
                        description = item.get("snippet", {}).get("description", "").lower()
                        is_short = (
                            _is_youtube_short_duration(duration) or
                            "short" in title or
                            "short" in description
                        )
                    
                    if is_short:
                        short_dates.append(dt_local)
                    else:
                        long_dates.append(dt_local)
    
        last_long = max(long_dates) if long_dates else None
        last_short = max(short_dates) if short_dates else None
        return last_long, last_short, ""
    except HttpError as he:
        try:
            msg = json.loads(he.content).get("error", {}).get("message", str(he))
        except Exception:
            msg = str(he)
        return None, None, f"YouTube API Error: {msg}"
    except Exception as e:
        return None, None, str(e)


def _is_youtube_short_duration(iso_duration: str) -> bool:
    """Return True if an ISO 8601 duration is 60 seconds or less."""
    import re
    if not iso_duration:
        return False
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not m:
        return False
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total <= 60


def get_last_facebook_scheduled_date(page_id: str, page_access_token: str) -> tuple[datetime.datetime | None, str]:
    """
    Fetch the latest scheduled publish time across all scheduled videos on the page.
    Returns (datetime, error_message). datetime is in local time.
    """
    timestamps = []
    errors = []
    
    # 1. Query /{page_id}/videos
    try:
        url = f"https://graph.facebook.com/v26.0/{page_id}/videos"
        params = {
            "access_token": page_access_token,
            "fields": "scheduled_publish_time,title",
            "limit": 50,
        }
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if "error" in data:
            errors.append(f"videos edge: {data['error'].get('message', 'Unknown FB error')}")
        else:
            for v in data.get("data", []):
                t = v.get("scheduled_publish_time")
                if t:
                    try:
                        timestamps.append(int(t))
                    except Exception:
                        pass
    except Exception as e:
        errors.append(f"videos edge query failed: {e}")
        
    # 2. Query /{page_id}/scheduled_posts
    try:
        url = f"https://graph.facebook.com/v26.0/{page_id}/scheduled_posts"
        params = {
            "access_token": page_access_token,
            "fields": "scheduled_publish_time,message",
            "limit": 50,
        }
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if "error" in data:
            errors.append(f"scheduled_posts edge: {data['error'].get('message', 'Unknown FB error')}")
        else:
            for p in data.get("data", []):
                t = p.get("scheduled_publish_time")
                if t:
                    try:
                        timestamps.append(int(t))
                    except Exception:
                        pass
    except Exception as e:
        errors.append(f"scheduled_posts edge query failed: {e}")

    if not timestamps:
        if errors:
            return None, "; ".join(errors)
        return None, "No scheduled videos or posts found on this Facebook page."
        
    latest_ts = max(timestamps)
    dt = datetime.datetime.fromtimestamp(latest_ts)
    return dt, ""


def get_facebook_page_token(user_token: str, page_id: str) -> tuple[str, str]:
    """
    Retrieve a never-expiring Page Access Token for page_id using a User Access Token.
    Returns (page_access_token, error_message).
    """
    if not user_token or not page_id:
        return "", "User Access Token and Page ID are required."

    try:
        page_url = f"https://graph.facebook.com/v26.0/{page_id}"
        page_params = {
            "fields": "access_token",
            "access_token": user_token
        }
        page_res = requests.get(page_url, params=page_params, timeout=30)
        page_json = page_res.json()
        if "error" in page_json:
            return "", f"Facebook Page Access Token Error: {page_json['error'].get('message')}"
        
        page_access_token = page_json.get("access_token")
        if not page_access_token:
            return "", f"Could not retrieve Page Access Token for Page ID {page_id}. Make sure the user has admin/editor access to this page."
        
        return page_access_token, ""
    except Exception as e:
        return "", f"Failed to retrieve Page Access Token: {e}"


def exchange_facebook_token(
    app_id: str,
    app_secret: str,
    short_lived_token: str,
    page_id: str = ""
) -> tuple[str, str, str]:
    """
    Exchange a short-lived Facebook user token for a long-lived user token,
    and then (if page_id is provided) exchange that for a never-expiring Page Access Token.
    Returns (long_lived_user_token, page_access_token, error_message).
    """
    if not app_id or not app_secret or not short_lived_token:
        return "", "", "App ID, App Secret, and Short-Lived Token are required."

    try:
        # Step 1: Exchange short-lived token for long-lived user token
        url = "https://graph.facebook.com/v26.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token
        }
        res = requests.get(url, params=params, timeout=30)
        res_json = res.json()
        if "error" in res_json:
            return "", "", f"Facebook Token Exchange Error: {res_json['error'].get('message')}"

        long_lived_user_token = res_json.get("access_token")
        if not long_lived_user_token:
            return "", "", "Facebook API did not return a long-lived user token."

        # Step 2: If page_id is provided, request the Page Access Token
        if page_id:
            page_token, err = get_facebook_page_token(long_lived_user_token, page_id)
            return long_lived_user_token, page_token, err
        
        return long_lived_user_token, "", ""
    except Exception as e:
        return "", "", f"Network or execution error: {e}"


def run_facebook_oauth_flow(
    app_id: str,
    app_secret: str,
    page_id: str = "",
    port: int = 5050,
    log_callback=None
) -> tuple[str, str, str, str]:
    """
    Start a local server to handle Facebook OAuth callback, open browser for user login,
    capture the redirect authorization code, and exchange it for a long-lived page/user token.
    Returns (short_lived_token, long_lived_user_token, page_access_token, error_message).
    """
    import http.server
    import socketserver
    import urllib.parse
    import webbrowser
    import threading

    if not app_id or not app_secret:
        return "", "", "", "App ID and App Secret are required."

    auth_code = []
    server_error = []

    class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authorization Success!</h1><p>You can close this tab and return to the app.</p>")
            elif "error" in params:
                server_error.append(params.get("error_description", ["Unknown error"])[0])
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authorization Failed!</h1><p>Check the app for details.</p>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    redirect_uri = f"http://localhost:{port}/"
    httpd = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("localhost", port), OAuthCallbackHandler)
    except Exception as e:
        return "", "", "", f"Failed to start local server on port {port}: {e}"

    scopes = "pages_show_list,business_management,pages_read_engagement,pages_manage_posts"
    login_url = (
        f"https://www.facebook.com/v26.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={scopes}"
        f"&response_type=code"
    )

    if log_callback:
        log_callback("Opening browser for Facebook Login...")
    
    webbrowser.open(login_url)

    def run_server():
        try:
            httpd.handle_request()
        except Exception as e:
            server_error.append(str(e))

    t = threading.Thread(target=run_server)
    t.start()
    t.join(timeout=120)

    try:
        httpd.server_close()
    except Exception:
        pass

    if server_error:
        return "", "", "", f"OAuth Error: {server_error[0]}"

    if not auth_code:
        return "", "", "", "Authorization timed out or was cancelled by user."

    try:
        token_url = "https://graph.facebook.com/v26.0/oauth/access_token"
        token_params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "client_secret": app_secret,
            "code": auth_code[0]
        }
        res = requests.get(token_url, params=token_params, timeout=30)
        res_json = res.json()
        if "error" in res_json:
            return "", "", "", f"Code Exchange Error: {res_json['error'].get('message')}"

        short_lived_token = res_json.get("access_token")
        if not short_lived_token:
            return "", "", "", "Facebook did not return an access token."

        long_user_token, page_token, err = exchange_facebook_token(app_id, app_secret, short_lived_token, page_id)
        return short_lived_token, long_user_token, page_token, err
    except Exception as e:
        return "", "", "", f"Failed during code exchange: {e}"


def sync_facebook_post_with_youtube_url(conn, project_id: int, log_callback=None) -> tuple[bool, str]:
    """
    Finds the project's Facebook Video ID and Main YouTube Video ID from SQLite,
    renders the Facebook post template with the YouTube URL, and updates both
    the Facebook Video object description and the Timeline Feed Post message via Graph API.
    """
    cursor = conn.execute("""
        SELECT p.id, p.name, p.overlay_text, p.fb_post_body,
               l_fb.output_file AS fb_video_id,
               l_yt.output_file AS main_yt_video_id
        FROM projects p
        LEFT JOIN process_log l_fb ON p.id = l_fb.project_id AND l_fb.step_num = 10
        LEFT JOIN process_log l_yt ON p.id = l_yt.project_id AND l_yt.step_num = 11
        WHERE p.id = ?
    """, (project_id,))
    row = cursor.fetchone()
    if not row:
        return False, f"Project ID {project_id} not found."

    project = dict(row)
    fb_vid_id = project.get("fb_video_id")
    yt_vid_id = project.get("main_yt_video_id")

    if not fb_vid_id or fb_vid_id.startswith("Bypassed"):
        return False, "No valid Facebook Video ID found for this project."

    if not yt_vid_id or yt_vid_id.startswith("Bypassed"):
        return False, "No valid YouTube Video ID found for this project."

    page_id = db.get_setting(conn, "fb_page_id", "").strip()
    page_token = db.get_setting(conn, "fb_page_token", "").strip()
    template = db.get_setting(conn, "fb_post_template", "").strip()

    if not page_token:
        return False, "Facebook Page Access Token missing in database settings."

    if not template:
        template = "{{title}}\n\n{{body}}\n\nWatch on YouTube: {{youtube-url}}"

    yt_url = f"https://www.youtube.com/watch?v={yt_vid_id}"
    rendered_desc = render_template(template, project.get("fb_post_body") or "", project.get("name") or "", pill_badge=project.get("pill_badge") or "")
    rendered_desc = rendered_desc.replace("{{youtube-url}}", yt_url)
    rendered_desc = rendered_desc.replace("{{overlay_text}}", project.get("overlay_text") or "")
    while rendered_desc.startswith('""'):
        rendered_desc = rendered_desc[1:]

    if log_callback:
        log_callback(f"Syncing Facebook post description with YouTube URL ({yt_url})...")

    # 1. Update Video Object Description
    url_vid = f"https://graph.facebook.com/v26.0/{fb_vid_id}"
    res_vid = requests.post(url_vid, data={"access_token": page_token, "description": rendered_desc}, timeout=30).json()

    # 2. Get Feed Post ID & Update Timeline Feed Post message
    post_updated = False
    try:
        get_resp = requests.get(url_vid, params={"fields": "post_id", "access_token": page_token}, timeout=15).json()
        post_id = get_resp.get("post_id")
        if post_id:
            full_post_id = f"{page_id}_{post_id}" if "_" not in str(post_id) and page_id else str(post_id)
            url_post = f"https://graph.facebook.com/v26.0/{full_post_id}"
            res_post = requests.post(url_post, data={"access_token": page_token, "message": rendered_desc}, timeout=30).json()
            if res_post.get("success", False) or "id" in res_post:
                post_updated = True
    except Exception:
        pass

    if res_vid.get("success", False) or "id" in res_vid or post_updated:
        return True, f"Facebook Post & Video [{fb_vid_id}] updated with YouTube link!"
    else:
        err_msg = res_vid.get("error", {}).get("message", "Failed to update Facebook post.")
        return False, err_msg


def sync_youtube_short_with_main_url(conn, project_id: int, log_callback=None) -> tuple[bool, str]:
    """
    Finds the project's YouTube Short ID and Main YouTube Video ID from SQLite,
    renders the Short description template with the Main YouTube URL, and updates
    the YouTube Short snippet via YouTube API.
    """
    cursor = conn.execute("""
        SELECT p.id, p.name, p.overlay_text, p.short_description_body,
               l_yt.output_file AS main_yt_video_id,
               l_short.output_file AS short_yt_video_id
        FROM projects p
        LEFT JOIN process_log l_yt ON p.id = l_yt.project_id AND l_yt.step_num = 11
        LEFT JOIN process_log l_short ON p.id = l_short.project_id AND l_short.step_num = 12
        WHERE p.id = ?
    """, (project_id,))
    row = cursor.fetchone()
    if not row:
        return False, f"Project ID {project_id} not found."

    project = dict(row)
    short_vid_id = project.get("short_yt_video_id")
    main_vid_id = project.get("main_yt_video_id")

    if not short_vid_id or short_vid_id.startswith("Bypassed"):
        return False, "No valid YouTube Short ID found for this project."

    if not main_vid_id or main_vid_id.startswith("Bypassed"):
        return False, "No valid Main YouTube Video ID found for this project."

    yt_creds, err = get_youtube_client(conn)
    if not yt_creds:
        return False, f"YouTube Auth Error: {err}"

    youtube = build("youtube", "v3", credentials=yt_creds)
    main_yt_url = f"https://www.youtube.com/watch?v={main_vid_id}"

    try:
        v_resp = youtube.videos().list(part="snippet", id=short_vid_id).execute()
        items = v_resp.get("items", [])
        if not items:
            return False, f"Short [{short_vid_id}] not found on YouTube API."

        snippet = items[0]["snippet"]
        template = db.get_setting(conn, "short_desc_template", "").strip()
        if not template:
            template = (
                "Relaxing music to sooth the soul and help guide you to sleep.\n\n"
                "Channel:  @music_to_sleep_to  \n"
                "Visit here to view the full-length 4k Youtube video: {{youtube-url}}\n\n"
                "© {{year}} Music To Sleep To. All Rights Reserved.\n"
                "Made with the help of Suno."
            )

        body_input = project.get("short_description_body") or template
        rendered_desc = render_template(template, body_input, project.get("name") or "", yt_url=main_yt_url, overlay_text=project.get("overlay_text") or "", pill_badge=project.get("pill_badge") or "")

        if log_callback:
            log_callback(f"Syncing YouTube Short description with Main YouTube URL ({main_yt_url})...")

        snippet["description"] = rendered_desc
        youtube.videos().update(part="snippet", body={"id": short_vid_id, "snippet": snippet}).execute()
        return True, f"YouTube Short [{short_vid_id}] description updated with YouTube URL!"
    except Exception as e:
        return False, str(e)


def post_youtube_short_comment(conn, project_id: int, log_callback=None) -> tuple[bool, str]:
    """
    Posts a top-level comment on the project's YouTube Short linking to the Main YouTube video.
    """
    cursor = conn.execute("""
        SELECT p.id, p.name, p.overlay_text,
               l_yt.output_file AS main_yt_video_id,
               l_short.output_file AS short_yt_video_id
        FROM projects p
        LEFT JOIN process_log l_yt ON p.id = l_yt.project_id AND l_yt.step_num = 11
        LEFT JOIN process_log l_short ON p.id = l_short.project_id AND l_short.step_num = 12
        WHERE p.id = ?
    """, (project_id,))
    row = cursor.fetchone()
    if not row:
        return False, f"Project ID {project_id} not found."

    project = dict(row)
    short_vid_id = project.get("short_yt_video_id")
    main_vid_id = project.get("main_yt_video_id")

    if not short_vid_id or short_vid_id.startswith("Bypassed"):
        return False, "No valid YouTube Short ID found for this project."

    if not main_vid_id or main_vid_id.startswith("Bypassed"):
        return False, "No valid Main YouTube Video ID found for this project."

    yt_creds, err = get_youtube_client(conn)
    if not yt_creds:
        return False, f"YouTube Auth Error: {err}"

    youtube = build("youtube", "v3", credentials=yt_creds)
    main_yt_url = f"https://www.youtube.com/watch?v={main_vid_id}"

    original_publish_at = None
    was_private = False

    # Check video privacy status (if private/scheduled, temporarily set to unlisted to allow commenting)
    try:
        v_resp = youtube.videos().list(part="status", id=short_vid_id).execute()
        items = v_resp.get("items", [])
        if items:
            status_obj = items[0].get("status", {})
            privacy = status_obj.get("privacyStatus", "")
            original_publish_at = status_obj.get("publishAt")
            if privacy == "private":
                was_private = True
                if log_callback:
                    log_callback(f"Temporarily setting Short [{short_vid_id}] to unlisted to post comment...")
                status_update = {
                    "privacyStatus": "unlisted",
                    "selfDeclaredMadeForKids": False
                }
                if original_publish_at:
                    status_update["publishAt"] = None

                youtube.videos().update(
                    part="status",
                    body={
                        "id": short_vid_id,
                        "status": status_update
                    }
                ).execute()
    except Exception as st_err:
        if log_callback:
            log_callback(f"Warning checking/updating status for Short [{short_vid_id}]: {st_err}")

    try:
        # Check if a comment already exists on this Short
        try:
            c_resp = youtube.commentThreads().list(part="snippet", videoId=short_vid_id, maxResults=20).execute()
            for item in c_resp.get("items", []):
                top_comment = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {}).get("textOriginal", "")
                if main_vid_id in top_comment or main_yt_url in top_comment:
                    if log_callback:
                        log_callback(f"Comment with YouTube URL already exists on Short [{short_vid_id}].")
                    return True, "Comment already posted on YouTube Short."
        except Exception:
            pass

        tpl = db.get_setting(conn, "short_comment_template", "").strip()
        if not tpl:
            tpl = "Watch the full-length 4K video here: {{youtube-url}} 💤✨"

        comment_text = render_template(tpl, "", project.get("name") or "", yt_url=main_yt_url, overlay_text=project.get("overlay_text") or "", pill_badge=project.get("pill_badge") or "")

        if log_callback:
            log_callback(f"Posting comment on YouTube Short [{short_vid_id}]: '{comment_text}'...")

        body = {
            "snippet": {
                "videoId": short_vid_id,
                "topLevelComment": {
                    "snippet": {
                        "textOriginal": comment_text
                    }
                }
            }
        }
        youtube.commentThreads().insert(part="snippet", body=body).execute()
        return True, f"Posted comment on YouTube Short [{short_vid_id}]!"
    except Exception as e:
        return False, f"Failed to post comment on YouTube Short [{short_vid_id}]: {e}"
    finally:
        # Restore scheduled status if video was originally private/scheduled
        if was_private:
            try:
                restore_status = {
                    "privacyStatus": "private",
                    "selfDeclaredMadeForKids": False
                }
                if original_publish_at:
                    restore_status["publishAt"] = original_publish_at
                youtube.videos().update(
                    part="status",
                    body={
                        "id": short_vid_id,
                        "status": restore_status
                    }
                ).execute()
                if log_callback:
                    log_callback(f"Restored Short [{short_vid_id}] back to scheduled status ({original_publish_at}).")
            except Exception as res_err:
                if log_callback:
                    log_callback(f"Warning restoring scheduled status for Short [{short_vid_id}]: {res_err}")


# ---------------------------------------------------------------------------
# TikTok Direct Post API v2 Integration
# ---------------------------------------------------------------------------

def get_tiktok_client(conn) -> tuple[dict | None, str]:
    """
    Retrieve stored TikTok credentials from settings database,
    validate presence of access token or refresh token.
    Returns (dict_of_creds, error_message).
    """
    client_key = db.get_setting(conn, "tiktok_client_key", "").strip()
    client_secret = db.get_setting(conn, "tiktok_client_secret", "").strip()
    access_token = db.get_setting(conn, "tiktok_access_token", "").strip()
    refresh_token = db.get_setting(conn, "tiktok_refresh_token", "").strip()
    open_id = db.get_setting(conn, "tiktok_open_id", "").strip()

    if not access_token and not refresh_token:
        return None, "TikTok is not authorized. Please configure TikTok API credentials in Settings."

    creds = {
        "client_key": client_key,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "open_id": open_id,
    }
    return creds, ""


def refresh_tiktok_token(conn, client_key: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """
    Refresh a TikTok user access token using the stored refresh_token.
    Returns (new_access_token, error_message).
    """
    if not client_key or not client_secret or not refresh_token:
        return "", "Client Key, Client Secret, and Refresh Token are required to refresh."

    url = "https://open.tiktokapis.com/v2/oauth/token/"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=30)
        res_json = resp.json()
        err = res_json.get("error")
        if err:
            if isinstance(err, dict):
                code = err.get("code", "")
                msg = err.get("message", "Unknown error")
                if code not in ("ok", "", None, 0):
                    return "", f"TikTok Refresh Error: {msg} ({code})"
            elif isinstance(err, str) and err.lower() not in ("ok", "none", "0", ""):
                desc = res_json.get("error_description", err)
                return "", f"TikTok Refresh Error: {desc} ({err})"

        data_block = res_json.get("data") if isinstance(res_json.get("data"), dict) else res_json
        new_access_token = data_block.get("access_token") or res_json.get("access_token", "")
        new_refresh_token = data_block.get("refresh_token") or res_json.get("refresh_token", refresh_token)
        open_id = data_block.get("open_id") or res_json.get("open_id", "")

        if not new_access_token:
            return "", "TikTok did not return a valid access token."

        if conn:
            db.set_setting(conn, "tiktok_access_token", new_access_token)
            if new_refresh_token:
                db.set_setting(conn, "tiktok_refresh_token", new_refresh_token)
            if open_id:
                db.set_setting(conn, "tiktok_open_id", open_id)

        return new_access_token, ""
    except Exception as e:
        return "", f"Network error during TikTok token refresh: {e}"


def run_tiktok_oauth_flow(
    conn,
    client_key: str,
    client_secret: str,
    port: int = 8989,
    redirect_uri: str = "",
    log_callback=None
) -> tuple[bool, str]:
    """
    Start local HTTP server to handle TikTok OAuth redirect callback,
    open browser to authorize, exchange authorization code for access & refresh tokens,
    and persist them into the SQLite database.
    """
    import http.server
    import socketserver
    import urllib.parse
    import webbrowser
    import threading
    import secrets

    if not client_key or not client_secret:
        return False, "TikTok Client Key and Client Secret are required."

    auth_code = []
    server_error = []
    csrf_state = secrets.token_hex(16)

    class TikTokOAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>TikTok Authorization Successful!</h1><p>You can close this tab and return to the app.</p>")
            elif "error" in params:
                server_error.append(params.get("error_description", ["Unknown error"])[0])
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>TikTok Authorization Failed!</h1><p>Check the app for details.</p>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    if not redirect_uri:
        if conn:
            redirect_uri = db.get_setting(conn, "tiktok_redirect_uri", "").strip()
    if not redirect_uri:
        redirect_uri = f"http://localhost:{port}/"
    httpd = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("localhost", port), TikTokOAuthHandler)
    except Exception as e:
        return False, f"Failed to start local server on port {port}: {e}"

    import hashlib
    import base64

    # PKCE Generation. TikTok requires code_challenge as a hex-encoded SHA256
    # digest, not the standard base64url encoding used by most OAuth providers.
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    code_challenge = hashlib.sha256(code_verifier.encode("utf-8")).hexdigest()

    scope = "user.info.basic,video.upload,video.publish"
    login_url = (
        f"https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={client_key}"
        f"&scope={urllib.parse.quote(scope)}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&state={csrf_state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    if log_callback:
        log_callback("Opening browser for TikTok Authorization...")

    webbrowser.open(login_url)

    def run_server():
        try:
            httpd.handle_request()
        except Exception as e:
            server_error.append(str(e))

    t = threading.Thread(target=run_server)
    t.start()
    t.join(timeout=120)

    try:
        httpd.server_close()
    except Exception:
        pass

    if server_error:
        return False, f"TikTok OAuth Error: {server_error[0]}"

    if not auth_code:
        return False, "TikTok authorization timed out or was cancelled by user."

    try:
        token_url = "https://open.tiktokapis.com/v2/oauth/token/"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": auth_code[0],
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        res = requests.post(token_url, headers=headers, data=data, timeout=30)
        res_json = res.json()
        err = res_json.get("error")
        if err:
            if isinstance(err, dict):
                code = err.get("code", "")
                msg = err.get("message", "Unknown error")
                if code not in ("ok", "", None, 0):
                    return False, f"Token Exchange Error: {msg} ({code})"
            elif isinstance(err, str) and err.lower() not in ("ok", "none", "0", ""):
                desc = res_json.get("error_description", err)
                return False, f"Token Exchange Error: {desc} ({err})"

        data_block = res_json.get("data") if isinstance(res_json.get("data"), dict) else res_json
        access_token = data_block.get("access_token") or res_json.get("access_token", "")
        refresh_token = data_block.get("refresh_token") or res_json.get("refresh_token", "")
        open_id = data_block.get("open_id") or res_json.get("open_id", "")

        if not access_token:
            return False, f"TikTok did not return a valid access token. Response: {res_json}"

        db.set_setting(conn, "tiktok_client_key", client_key)
        db.set_setting(conn, "tiktok_client_secret", client_secret)
        db.set_setting(conn, "tiktok_access_token", access_token)
        if refresh_token:
            db.set_setting(conn, "tiktok_refresh_token", refresh_token)
        if open_id:
            db.set_setting(conn, "tiktok_open_id", open_id)

        return True, "TikTok authorization successful! Credentials saved."
    except Exception as e:
        return False, f"Error exchanging authorization code: {e}"


def upload_to_tiktok(
    access_token: str,
    video_path: str,
    title: str = "",
    description: str = "",
    schedule_time_iso: str = "",
    privacy_level: str = "SELF_ONLY",
    tags: list[str] = None,
    log_callback=None,
    conn=None
) -> tuple[str, str]:
    """
    Upload a video to TikTok via Direct Post / Content Posting API v2.
    Steps:
      1. Initialize video upload request (`/v2/post/publish/video/init/`)
      2. Stream video chunk(s) to TikTok's returned `upload_url` via HTTP PUT
      3. Poll publish status (`/v2/post/publish/status/fetch/`)
    Returns (publish_id, error_message).
    """
    if not os.path.isfile(video_path):
        return "", f"Video file not found: {video_path}"

    file_size = os.path.getsize(video_path)
    if file_size == 0:
        return "", f"Video file is empty: {video_path}"

    if conn and (not privacy_level or privacy_level == "SELF_ONLY"):
        privacy_level = db.get_setting(conn, "tiktok_privacy_level", "SELF_ONLY").strip() or "SELF_ONLY"

    # Build post caption
    caption = title.strip()
    if description.strip() and description.strip() != title.strip():
        caption = f"{caption}\n\n{description.strip()}" if caption else description.strip()
    if tags:
        tag_str = " ".join(f"#{t.strip('#')}" for t in tags if t.strip())
        if tag_str:
            caption = f"{caption}\n\n{tag_str}".strip()

    # TikTok caption length limit is 2200 characters
    if len(caption) > 2200:
        caption = caption[:2197] + "..."

    if log_callback:
        log_callback(f"Initializing TikTok Direct Post upload ({file_size / (1024 * 1024):.1f} MB, Privacy: {privacy_level})...")

    init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8"
    }

    # Setup post info
    post_info = {
        "title": caption,
        "privacy_level": privacy_level,
        "disable_duet": False,
        "disable_stitch": False,
        "disable_comment": False,
        "video_cover_timestamp_ms": 1000,
    }

    # Calculate chunk size and chunk count based on TikTok Direct Post API requirements:
    # 1. If file_size <= 64 MB, upload in 1 chunk with chunk_size = file_size.
    # 2. If file_size > 64 MB, chunk_size must be between 5 MB and 64 MB.
    #    TikTok requires: total_chunk_count = math.floor(file_size / chunk_size).
    #    The final chunk carries the remaining trailing bytes (up to 128 MB).
    MAX_SINGLE_CHUNK = 64 * 1024 * 1024  # 64 MB
    CHUNK_UNIT = 20 * 1024 * 1024        # 20 MB

    if file_size <= MAX_SINGLE_CHUNK:
        chunk_size = file_size
        total_chunk_count = 1
    else:
        chunk_size = CHUNK_UNIT
        total_chunk_count = max(1, file_size // chunk_size)

    payload = {
        "post_info": post_info,
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count
        }
    }

    try:
        init_res = requests.post(init_url, headers=headers, json=payload, timeout=30)
        init_json = init_res.json()
    except Exception as e:
        return "", f"TikTok init request failed: {e}"

    error_info = init_json.get("error", {})
    if error_info.get("code") not in ("ok", "", None, 0):
        err_msg = error_info.get("message", "Unknown error")
        err_code = error_info.get("code", "")

        if err_code == "unaudited_client_can_only_post_to_private_accounts":
            return "", (
                "TikTok Direct Post Error: Unaudited developer apps in Development Mode "
                "can only publish videos to TikTok accounts set to 'Private'. "
                "Please toggle 'Private Account' ON in your TikTok Mobile App "
                "(Profile -> Settings and privacy -> Privacy -> Private account = ON), "
                "or submit your App for audit in the TikTok Developer Portal."
            )

        # If token expired and conn available, attempt automatic refresh
        if "token" in err_msg.lower() or "auth" in err_msg.lower() or err_code == "access_token_invalid":
            if conn:
                ck = db.get_setting(conn, "tiktok_client_key", "").strip()
                cs = db.get_setting(conn, "tiktok_client_secret", "").strip()
                rt = db.get_setting(conn, "tiktok_refresh_token", "").strip()
                if ck and cs and rt:
                    if log_callback:
                        log_callback("TikTok access token expired. Attempting token refresh...")
                    new_token, ref_err = refresh_tiktok_token(conn, ck, cs, rt)
                    if new_token:
                        return upload_to_tiktok(
                            new_token, video_path, title, description,
                            schedule_time_iso, privacy_level, tags, log_callback, conn=None
                        )
                    else:
                        return "", f"TikTok token refresh failed: {ref_err}"
        return "", f"TikTok init error: {err_msg} ({err_code})"

    data_block = init_json.get("data", {})
    publish_id = data_block.get("publish_id", "")
    upload_url = data_block.get("upload_url", "")

    if not upload_url:
        return "", f"TikTok did not return upload URL: {init_json}"

    if log_callback:
        log_callback(f"Streaming video to TikTok upload endpoint (Publish ID: {publish_id})...")

    class ProgressStream:
        def __init__(self, file_obj, total_bytes, callback=None, label="TikTok"):
            self._file = file_obj
            self._total = total_bytes
            self._callback = callback
            self._label = label
            self._read_bytes = 0
            self._last_pct = -1

        def read(self, chunk_size=8192):
            chunk = self._file.read(chunk_size)
            if chunk:
                self._read_bytes += len(chunk)
                if self._total > 0 and self._callback:
                    pct = int((self._read_bytes / self._total) * 100)
                    if pct != self._last_pct and (pct % 5 == 0 or pct == 100):
                        self._last_pct = pct
                        self._callback(f"Uploading to {self._label}: {pct}% complete...")
            return chunk

    # Upload video binary to upload_url (supports single or multi-chunk uploads)
    try:
        with open(video_path, "rb") as f:
            if total_chunk_count == 1:
                upload_headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(file_size),
                    "Content-Range": f"bytes 0-{file_size - 1}/{file_size}"
                }
                stream = ProgressStream(f, file_size, callback=log_callback, label="TikTok")
                put_res = requests.put(upload_url, headers=upload_headers, data=stream, timeout=300)
                if put_res.status_code not in (200, 201, 204, 206, 308):
                    return "", f"TikTok video upload failed (HTTP {put_res.status_code}): {put_res.text}"
            else:
                uploaded_bytes = 0
                for i in range(total_chunk_count):
                    start_byte = i * chunk_size
                    if i == total_chunk_count - 1:
                        end_byte = file_size - 1
                    else:
                        end_byte = start_byte + chunk_size - 1
                    
                    current_chunk_len = end_byte - start_byte + 1
                    chunk_data = f.read(current_chunk_len)
                    
                    upload_headers = {
                        "Content-Type": "video/mp4",
                        "Content-Length": str(current_chunk_len),
                        "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}"
                    }
                    put_res = requests.put(upload_url, headers=upload_headers, data=chunk_data, timeout=300)
                    if put_res.status_code not in (200, 201, 204, 206, 308):
                        return "", f"TikTok chunk {i + 1}/{total_chunk_count} upload failed (HTTP {put_res.status_code}): {put_res.text}"
                    
                    uploaded_bytes += current_chunk_len
                    if log_callback:
                        pct = int((uploaded_bytes / file_size) * 100)
                        log_callback(f"Uploading to TikTok: {pct}% complete (Chunk {i + 1}/{total_chunk_count})...")
    except Exception as e:
        return "", f"Error streaming video to TikTok: {e}"

    if log_callback:
        log_callback("Video bytes transferred. Verifying TikTok publish status...")

    # Poll status
    status_url = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
    status_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8"
    }

    for attempt in range(12):
        time.sleep(3)
        try:
            st_res = requests.post(status_url, headers=status_headers, json={"publish_id": publish_id}, timeout=30)
            st_json = st_res.json()
            st_data = st_json.get("data", {})
            status = st_data.get("status", "")
            fail_reason = st_data.get("fail_reason", "")

            if log_callback:
                log_callback(f"TikTok status check {attempt + 1}: {status}")

            if status in ("PUBLISH_COMPLETE", "SUCCESS"):
                if log_callback:
                    log_callback(f"TikTok video published successfully! Publish ID: {publish_id}")
                return publish_id, ""
            elif status == "FAILED":
                return "", f"TikTok video publish failed: {fail_reason or 'Unknown error'}"
            elif status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD", "SENDING_TO_USER"):
                continue
        except Exception as e:
            if log_callback:
                log_callback(f"Status poll warning: {e}")

    # If polling times out but was submitted successfully, return publish_id with notice
    if log_callback:
        log_callback(f"TikTok upload complete (Publish ID: {publish_id}). Status is processing.")
    return publish_id, ""


# ---------------------------------------------------------------------------
# Zernio Integration (unified social API, used to Direct Post to TikTok
# without needing this app's own TikTok developer client to pass TikTok's
# "wide audience of creators" Content Posting API audit)
# ---------------------------------------------------------------------------

ZERNIO_API_BASE = "https://zernio.com/api/v1"


def _zernio_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def get_zernio_client(conn) -> tuple[dict | None, str]:
    """
    Retrieve stored Zernio credentials (API key + connected TikTok account)
    from the settings database. Returns (dict_of_creds, error_message).
    """
    api_key = db.get_setting(conn, "zernio_api_key", "").strip()
    if not api_key:
        return None, "Zernio is not configured. Please enter your Zernio API Key in Settings."

    account_id = db.get_setting(conn, "zernio_tiktok_account_id", "").strip()
    if not account_id:
        return None, "Zernio TikTok account is not connected. Please connect it in Settings."

    return {
        "api_key": api_key,
        "profile_id": db.get_setting(conn, "zernio_profile_id", "").strip(),
        "account_id": account_id,
        "username": db.get_setting(conn, "zernio_tiktok_username", "").strip(),
    }, ""


def get_zernio_default_profile_id(api_key: str) -> tuple[str, str]:
    """Fetch the default (or first) Zernio profile id for this API key."""
    try:
        res = requests.get(f"{ZERNIO_API_BASE}/profiles", headers=_zernio_headers(api_key), timeout=20)
        data = res.json()
    except Exception as e:
        return "", f"Failed to reach Zernio: {e}"

    profiles = data.get("profiles") if isinstance(data, dict) else data
    if not profiles:
        return "", f"No Zernio profile found for this API key: {data}"

    default_profile = next((p for p in profiles if p.get("isDefault") or p.get("default")), profiles[0])
    profile_id = default_profile.get("_id") or default_profile.get("id") or ""
    if not profile_id:
        return "", f"Could not determine Zernio profile id from response: {data}"
    return profile_id, ""


def run_zernio_tiktok_connect(conn, api_key: str, port: int = 8990, log_callback=None) -> tuple[bool, str]:
    """
    Open the browser to Zernio's hosted TikTok OAuth flow, wait for the
    redirect back to a local callback server, then read the newly connected
    TikTok account from Zernio and persist its account id.
    """
    import http.server
    import socketserver
    import webbrowser
    import threading

    if not api_key:
        return False, "Zernio API Key is required."

    profile_id, err = get_zernio_default_profile_id(api_key)
    if not profile_id:
        return False, err

    got_callback = []

    class ConnectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got_callback.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>TikTok Connected via Zernio!</h1><p>You can close this tab and return to the app.</p>")

        def log_message(self, format, *args):
            pass

    httpd = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("localhost", port), ConnectHandler)
    except Exception as e:
        return False, f"Failed to start local server on port {port}: {e}"

    redirect_url = f"http://localhost:{port}/callback"
    try:
        res = requests.get(
            f"{ZERNIO_API_BASE}/connect/tiktok",
            headers=_zernio_headers(api_key),
            params={"profileId": profile_id, "redirect_url": redirect_url},
            timeout=20
        )
        conn_json = res.json()
    except Exception as e:
        httpd.server_close()
        return False, f"Failed to start Zernio TikTok connection: {e}"

    auth_url = conn_json.get("authUrl") or conn_json.get("url") or conn_json.get("connectUrl")
    if not auth_url:
        httpd.server_close()
        return False, f"Zernio did not return a connection URL: {conn_json}"

    if log_callback:
        log_callback("Opening browser to connect TikTok via Zernio...")
    webbrowser.open(auth_url)

    def run_server():
        try:
            httpd.handle_request()
        except Exception:
            pass

    t = threading.Thread(target=run_server)
    t.start()
    t.join(timeout=180)
    try:
        httpd.server_close()
    except Exception:
        pass

    if not got_callback:
        return False, "Timed out waiting for the TikTok connection to complete in your browser."

    # Fetch the newly connected TikTok account from Zernio
    try:
        acc_res = requests.get(
            f"{ZERNIO_API_BASE}/accounts",
            headers=_zernio_headers(api_key),
            params={"profileId": profile_id},
            timeout=20
        )
        acc_json = acc_res.json()
    except Exception as e:
        return False, f"Connected, but failed to fetch account list from Zernio: {e}"

    accounts = acc_json.get("accounts") if isinstance(acc_json, dict) else acc_json
    if not accounts:
        return False, f"No accounts returned by Zernio after connecting: {acc_json}"

    tiktok_accounts = [a for a in accounts if str(a.get("platform", "")).lower() == "tiktok"]
    if not tiktok_accounts:
        return False, f"TikTok account not found in Zernio's account list: {acc_json}"

    account = tiktok_accounts[-1]
    account_id = account.get("accountId") or account.get("_id") or account.get("id") or ""
    username = account.get("username") or account.get("name") or ""

    if not account_id:
        return False, f"Could not determine account id from Zernio response: {account}"

    if conn:
        db.set_setting(conn, "zernio_api_key", api_key)
        db.set_setting(conn, "zernio_profile_id", profile_id)
        db.set_setting(conn, "zernio_tiktok_account_id", account_id)
        db.set_setting(conn, "zernio_tiktok_username", username)

    return True, f"TikTok connected via Zernio successfully!{f' (@{username})' if username else ''}"


def upload_via_zernio_tiktok(
    api_key: str,
    account_id: str,
    video_path: str,
    caption: str = "",
    schedule_time_iso: str = "",
    privacy_level: str = "SELF_ONLY",
    log_callback=None,
) -> tuple[str, str]:
    """
    Upload and publish a video to TikTok through Zernio's unified posting API,
    uploading the file directly to Zernio's own presign+upload endpoint.
    Returns (post_id, error_message).
    """
    if not api_key:
        return "", "Zernio API Key is required."
    if not account_id:
        return "", "Zernio TikTok account is not connected."
    if not os.path.isfile(video_path):
        return "", f"Video file not found: {video_path}"

    if os.path.getsize(video_path) == 0:
        return "", f"Video file is empty: {video_path}"

    import tempfile
    import subprocess
    from pipeline import get_binary_path

    # Always normalize to H.264/AAC/1080x1920/yuv420p -- never pass the
    # source through untouched. This pipeline's own renders don't
    # consistently produce the same codec (some steps re-encode with
    # h264_videotoolbox/libx264, others stream-copy the source's original
    # codec via "-c:v copy"), so a conditional "only transcode if
    # non-standard" check here previously let some already-H.264 files skip
    # normalization and upload whatever audio encoding that render happened
    # to have -- which is why TikTok played some videos silently and others
    # fine despite them looking identical locally.
    actual_video_path = video_path
    temp_transcoded = None
    try:
        ffprobe_bin = get_binary_path("ffprobe")
        probe_cmd = [
            ffprobe_bin, "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt",
            "-of", "json", video_path
        ]
        probe_out = subprocess.check_output(probe_cmd, stderr=subprocess.DEVNULL)
        probe_json = json.loads(probe_out)
        streams = probe_json.get("streams", [])
        v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        v_codec = v_stream.get("codec_name", "").lower()
        v_w = int(v_stream.get("width", 0) or 0)
        v_h = int(v_stream.get("height", 0) or 0)
        v_pix = v_stream.get("pix_fmt", "").lower()

        if log_callback:
            log_callback(f"Optimizing video for TikTok ({v_codec} {v_w}x{v_h} {v_pix} -> H.264 1080x1920 yuv420p)...")

        ffmpeg_bin = get_binary_path("ffmpeg")
        temp_transcoded = os.path.join(tempfile.gettempdir(), f"tiktok_{int(time.time())}.mp4")
        conv_cmd = [
            ffmpeg_bin, "-y", "-i", video_path,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.0",
            "-crf", "22", "-pix_fmt", "yuv420p", "-color_range", "tv",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        ]
        conv_cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"] if has_audio else ["-an"]
        conv_cmd += ["-movflags", "+faststart", temp_transcoded]
        subprocess.run(conv_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.isfile(temp_transcoded):
            actual_video_path = temp_transcoded
    except Exception as probe_err:
        if log_callback:
            log_callback(f"Format check note: using source video ({probe_err})")

    file_size = os.path.getsize(actual_video_path)

    caption = (caption or "").strip()
    if len(caption) > 2200:
        caption = caption[:2197] + "..."

    # TikTok strips any line that is completely empty, collapsing intentional
    # blank-line spacing between paragraphs. Placing an invisible character
    # (Braille Pattern Blank) on those lines keeps them from being empty, so
    # TikTok preserves the line break.
    caption = "\n".join(ln if ln.strip() else "⠀" for ln in caption.split("\n"))

    headers = _zernio_headers(api_key)

    # Parse the requested schedule time. A time already in the past (e.g. a
    # stale value left over in the project from an earlier test) is treated
    # as no schedule at all -- publish immediately instead of sending TikTok
    # a bogus past "scheduledFor" date.
    schedule_dt_utc = None
    if schedule_time_iso:
        try:
            clean_iso = schedule_time_iso
            if not clean_iso.endswith("Z") and "+" not in clean_iso and "-" not in clean_iso[10:]:
                dt = datetime.datetime.fromisoformat(clean_iso)
                local_tz = datetime.datetime.now().astimezone().tzinfo
                parsed_dt_utc = dt.replace(tzinfo=local_tz).astimezone(datetime.timezone.utc)
            else:
                parsed_dt_utc = datetime.datetime.fromisoformat(clean_iso.replace("Z", "+00:00"))

            if parsed_dt_utc > datetime.datetime.now(datetime.timezone.utc):
                schedule_dt_utc = parsed_dt_utc
            elif log_callback:
                log_callback(f"Note: schedule time '{schedule_time_iso}' is in the past; publishing now instead.")
        except Exception:
            schedule_dt_utc = None

    try:
        if log_callback:
            log_callback(f"Requesting Zernio upload URL ({file_size / (1024 * 1024):.1f} MB)...")
        try:
            presign_res = requests.post(
                f"{ZERNIO_API_BASE}/media/presign",
                headers=headers,
                json={
                    "filename": os.path.basename(actual_video_path),
                    "contentType": "video/mp4",
                    "size": file_size,
                },
                timeout=30,
            )
            presign_json = presign_res.json()
        except Exception as e:
            return "", f"Zernio presign request failed: {e}"

        upload_url = presign_json.get("uploadUrl")
        public_url = presign_json.get("publicUrl")
        if not upload_url or not public_url:
            return "", f"Zernio did not return an upload URL: {presign_json}"

        if log_callback:
            log_callback("Uploading video to Zernio...")

        # The presigned upload URL is valid for an hour, so a few retries on
        # transient network/TLS errors (e.g. SSLEOFError mid-transfer on a
        # large file) are safe -- just re-read the file fresh each attempt.
        max_attempts = 3
        last_err = None
        for attempt in range(1, max_attempts + 1):
            try:
                with open(actual_video_path, "rb") as f:
                    put_res = requests.put(upload_url, headers={"Content-Type": "video/mp4"}, data=f, timeout=600)
                if put_res.status_code in (200, 201, 204):
                    last_err = None
                    break
                last_err = f"HTTP {put_res.status_code}: {put_res.text}"
            except Exception as e:
                last_err = str(e)

            if attempt < max_attempts:
                if log_callback:
                    log_callback(f"Upload to Zernio failed ({last_err}); retrying ({attempt}/{max_attempts - 1})...")
                time.sleep(5 * attempt)

        if last_err:
            return "", f"Zernio media upload failed after {max_attempts} attempts: {last_err}"

        post_payload = {
            "content": caption,
            "mediaItems": [{"url": public_url, "type": "video"}],
            "platforms": [{"platform": "tiktok", "accountId": account_id}],
            "tiktokSettings": {
                "privacy_level": privacy_level or "SELF_ONLY",
                "allow_comment": True,
                "allow_duet": True,
                "allow_stitch": True,
                "content_preview_confirmed": True,
                "express_consent_given": True,
            },
        }

        if schedule_dt_utc:
            post_payload["scheduledFor"] = schedule_dt_utc.isoformat().replace("+00:00", "Z")
            post_payload["timezone"] = "UTC"
        elif schedule_time_iso:
            # Schedule time provided but couldn't be parsed; fall back to publishing now.
            if log_callback:
                log_callback(f"Warning: Could not parse schedule time '{schedule_time_iso}' for Zernio; publishing now.")
            post_payload["publishNow"] = True
        else:
            post_payload["publishNow"] = True

        if log_callback:
            log_callback("Publishing TikTok post via Zernio...")
        try:
            post_res = requests.post(f"{ZERNIO_API_BASE}/posts", headers=headers, json=post_payload, timeout=60)
            post_json = post_res.json()
        except Exception as e:
            return "", f"Zernio post creation failed: {e}"

        post = post_json.get("post") if isinstance(post_json, dict) else None
        if not post:
            return "", f"Zernio did not return a created post: {post_json}"

        post_id = post.get("_id") or post.get("id") or ""
        status = post.get("status", "")

        if status == "failed":
            return "", f"Zernio reported the post failed: {post}"

        if log_callback:
            log_callback(f"Zernio post {status or 'submitted'} (ID: {post_id}).")

        return post_id, ""
    finally:
        if temp_transcoded and os.path.isfile(temp_transcoded):
            try:
                os.remove(temp_transcoded)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Instagram Integration (Meta Graph API / Content Publishing API)
# ---------------------------------------------------------------------------

def run_instagram_oauth_flow(
    conn,
    app_id: str,
    app_secret: str,
    port: int = 5055,
    redirect_uri: str = "",
    log_callback=None
) -> tuple[bool, str]:
    """
    Start a local HTTP server on port 5055 to handle Meta/Instagram OAuth redirect,
    open browser for user authorization, exchange code for 60-day long-lived token,
    automatically discover linked Instagram Business/Creator accounts, and persist
    credentials directly into SQLite database settings.
    """
    import http.server
    import socketserver
    import urllib.parse
    import webbrowser
    import threading

    if not app_id or not app_secret:
        return False, "Meta App ID and App Secret are required."

    auth_code = []
    server_error = []

    class InstagramOAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                auth_code.append(params["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Instagram Authorization Successful!</h1><p>You can close this tab and return to Music Video Pipeline Manager.</p>")
            elif "error" in params:
                server_error.append(params.get("error_description", ["Unknown error"])[0])
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Instagram Authorization Failed!</h1><p>Check the app for details.</p>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    if not redirect_uri:
        if conn:
            redirect_uri = db.get_setting(conn, "instagram_redirect_uri", "").strip()
    if not redirect_uri:
        redirect_uri = f"http://localhost:{port}/"

    httpd = None
    try:
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("localhost", port), InstagramOAuthHandler)
    except Exception as e:
        return False, f"Failed to start local server on port {port}: {e}"

    scopes = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"
    login_url = (
        f"https://www.facebook.com/v26.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={scopes}"
        f"&response_type=code"
    )

    if log_callback:
        log_callback("Opening browser for Instagram / Meta Authorization...")

    webbrowser.open(login_url)

    def run_server():
        try:
            httpd.handle_request()
        except Exception as e:
            server_error.append(str(e))

    t = threading.Thread(target=run_server)
    t.start()
    t.join(timeout=120)

    try:
        httpd.server_close()
    except Exception:
        pass

    if server_error:
        return False, f"Instagram OAuth Error: {server_error[0]}"

    if not auth_code:
        return False, "Instagram authorization timed out or was cancelled by user."

    try:
        # 1. Exchange auth code for short-lived user token
        token_url = "https://graph.facebook.com/v26.0/oauth/access_token"
        token_params = {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "client_secret": app_secret,
            "code": auth_code[0]
        }
        res = requests.get(token_url, params=token_params, timeout=30)
        res_json = res.json()
        if "error" in res_json:
            return False, f"Token Exchange Error: {res_json['error'].get('message')}"

        short_lived_token = res_json.get("access_token")
        if not short_lived_token:
            return False, "Meta did not return an access token."

        # 2. Exchange short-lived token for 60-day long-lived token
        exchange_params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token
        }
        res_ex = requests.get(token_url, params=exchange_params, timeout=30)
        res_ex_json = res_ex.json()
        long_lived_token = res_ex_json.get("access_token", short_lived_token)

        # 3. Discover linked Instagram Business / Creator accounts
        accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
        acc_params = {
            "fields": "id,name,access_token,instagram_business_account{id,username,name}",
            "access_token": long_lived_token
        }
        acc_res = requests.get(accounts_url, params=acc_params, timeout=30)
        acc_json = acc_res.json()

        found_ig_id = ""
        found_ig_username = ""
        page_token = ""

        if "data" in acc_json and isinstance(acc_json["data"], list):
            for page in acc_json["data"]:
                ig_acc = page.get("instagram_business_account")
                if ig_acc and ig_acc.get("id"):
                    found_ig_id = ig_acc["id"]
                    found_ig_username = ig_acc.get("username", "")
                    page_token = page.get("access_token", "")
                    break

        # Persist credentials into database settings
        effective_token = page_token or long_lived_token
        if conn:
            db.set_setting(conn, "instagram_app_id", app_id)
            db.set_setting(conn, "instagram_app_secret", app_secret)
            db.set_setting(conn, "instagram_redirect_uri", redirect_uri)
            db.set_setting(conn, "instagram_access_token", effective_token)
            if found_ig_id:
                db.set_setting(conn, "instagram_account_id", found_ig_id)
            if found_ig_username:
                db.set_setting(conn, "instagram_username", found_ig_username)

        if found_ig_id:
            user_display = f"@{found_ig_username}" if found_ig_username else f"ID: {found_ig_id}"
            return True, f"Successfully connected to Instagram Account {user_display}!"
        else:
            return True, "Authorized Meta account, but no linked Instagram Business/Creator account was found on your Facebook Pages. Please link your Instagram account to a Facebook Page in Meta Business Suite."
    except Exception as e:
        return False, f"Error completing Instagram authentication: {e}"


def exchange_instagram_token(conn, app_id: str, app_secret: str, token_input: str) -> tuple[bool, str]:
    """
    Exchange a short-lived token or process a user token from Graph API Explorer,
    upgrade it to a 60-day or permanent page-linked token, discover the linked
    Instagram Business / Creator account, and persist into database settings.
    """
    if not token_input or not token_input.strip():
        return False, "Token cannot be empty."

    token_str = token_input.strip()
    long_lived_token = token_str

    # Attempt exchange for 60-day token if app_id & app_secret provided
    if app_id and app_secret:
        try:
            token_url = "https://graph.facebook.com/v26.0/oauth/access_token"
            exchange_params = {
                "grant_type": "fb_exchange_token",
                "client_id": app_id.strip(),
                "client_secret": app_secret.strip(),
                "fb_exchange_token": token_str
            }
            res_ex = requests.get(token_url, params=exchange_params, timeout=30)
            res_ex_json = res_ex.json()
            if "access_token" in res_ex_json:
                long_lived_token = res_ex_json["access_token"]
        except Exception:
            pass

    # Discover linked Instagram Business / Creator accounts from Pages
    accounts_url = "https://graph.facebook.com/v26.0/me/accounts"
    acc_params = {
        "fields": "id,name,access_token,instagram_business_account{id,username,name}",
        "access_token": long_lived_token
    }
    try:
        acc_res = requests.get(accounts_url, params=acc_params, timeout=30)
        acc_json = acc_res.json()
        if "error" in acc_json:
            return False, f"Meta API Error: {acc_json['error'].get('message')}"
    except Exception as e:
        return False, f"Failed to query connected accounts: {e}"

    found_ig_id = ""
    found_ig_username = ""
    page_name = ""
    page_token = ""

    if "data" in acc_json and isinstance(acc_json["data"], list):
        for page in acc_json["data"]:
            ig_acc = page.get("instagram_business_account")
            if ig_acc and ig_acc.get("id"):
                found_ig_id = ig_acc["id"]
                found_ig_username = ig_acc.get("username", "")
                page_name = page.get("name", "")
                page_token = page.get("access_token", "")
                break

    effective_token = page_token or long_lived_token
    if conn:
        db.set_setting(conn, "instagram_access_token", effective_token)
        if found_ig_id:
            db.set_setting(conn, "instagram_account_id", found_ig_id)
        if found_ig_username:
            db.set_setting(conn, "instagram_username", found_ig_username)

    if found_ig_id:
        return True, f"Successfully connected to Instagram @{found_ig_username} (ID: {found_ig_id}) via Facebook Page '{page_name}'!"
    else:
        return True, "Token saved successfully! (Note: No linked Instagram Business/Creator account found on your Facebook Pages yet. Ensure your Instagram account is connected to your Facebook Page in Page Settings)."


def get_instagram_client(conn) -> tuple[dict | None, str]:
    """
    Validates and returns stored Instagram credentials.
    Returns (creds_dict, error_message).
    """
    account_id = db.get_setting(conn, "instagram_account_id", "").strip()
    access_token = db.get_setting(conn, "instagram_access_token", "").strip() or db.get_setting(conn, "fb_page_token", "").strip()
    username = db.get_setting(conn, "instagram_username", "").strip()

    if not access_token:
        return None, "Instagram Access Token is not set. Please Authorize Instagram in Settings."

    if not account_id:
        return None, "Instagram Account ID is not set. Please link an Instagram Business/Creator account in Settings."

    # Validate token scopes and account
    try:
        # Check token scopes via debug_token
        try:
            dbg_url = "https://graph.facebook.com/debug_token"
            dbg_res = requests.get(dbg_url, params={"input_token": access_token, "access_token": access_token}, timeout=10).json()
            scopes = dbg_res.get("data", {}).get("scopes", [])
            if scopes:
                missing = []
                if "instagram_basic" not in scopes:
                    missing.append("instagram_basic")
                if "instagram_content_publish" not in scopes:
                    missing.append("instagram_content_publish")
                if missing:
                    return None, (
                        f"Your Access Token is missing required Instagram permissions:\n• " + "\n• ".join(missing) +
                        "\n\nIn Meta Graph API Explorer, click 'Add a Permission', add the missing permission(s) above, and click 'Generate Access Token'."
                    )
        except Exception:
            pass

        val_url = f"https://graph.facebook.com/v26.0/{account_id}"
        val_params = {
            "fields": "id,username,name",
            "access_token": access_token
        }
        res = requests.get(val_url, params=val_params, timeout=15)
        res_json = res.json()
        if "error" in res_json:
            return None, f"Instagram validation error: {res_json['error'].get('message')}"

        acc_username = res_json.get("username") or username
        return {
            "account_id": account_id,
            "username": acc_username,
            "access_token": access_token,
        }, ""
    except Exception as e:
        # Fallback to stored credentials if offline/network hiccup
        return {
            "account_id": account_id,
            "username": username,
            "access_token": access_token,
        }, ""


class _UploadProgressReader:
    """File-like wrapper that streams binary data for requests.post while reporting progress."""
    def __init__(self, file_path: str, callback=None):
        self.file_path = file_path
        self.total_size = os.path.getsize(file_path)
        self.f = open(file_path, "rb")
        self.uploaded_bytes = 0
        self.callback = callback
        self.last_pct = -1

    def read(self, size=-1):
        chunk = self.f.read(size)
        if chunk:
            self.uploaded_bytes += len(chunk)
            pct = min(100, int((self.uploaded_bytes / self.total_size) * 100))
            if self.callback and pct != self.last_pct:
                self.last_pct = pct
                self.callback(pct)
        return chunk

    def seek(self, offset, whence=0):
        return self.f.seek(offset, whence)

    def tell(self):
        return self.f.tell()

    def __len__(self):
        return self.total_size

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def upload_to_instagram_reel(
    access_token: str,
    ig_user_id: str,
    video_path: str,
    caption: str = "",
    schedule_time_iso: str = "",
    log_callback=None,
    gcs_bucket: str = "",
    gcs_key_path: str = ""
) -> tuple[str, str]:
    """
    Upload a vertical 9:16 video to Instagram as a Reel using either:
    1. Google Cloud Storage signed URL (fast-path ingestion), or
    2. Meta Graph API's resumable container binary stream protocol.
    Returns (published_media_id, error_message).
    """
    if not access_token or not ig_user_id:
        return "", "Instagram Access Token and Account ID are required."

    if not os.path.isfile(video_path):
        return "", f"Video file not found: {video_path}"

    import sys
    import tempfile
    import subprocess
    from pipeline import get_binary_path

    # Check GCS settings from arguments or database
    use_gcs = False
    actual_gcs_bucket = gcs_bucket
    actual_gcs_key_path = gcs_key_path

    try:
        import sqlite3
        conn = sqlite3.connect("pipeline_data/pipeline.db")
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key IN ('instagram_use_gcs', 'instagram_gcs_bucket', 'instagram_gcs_key_path')")
        gcs_settings = dict(c.fetchall())
        conn.close()

        if not actual_gcs_bucket:
            actual_gcs_bucket = gcs_settings.get("instagram_gcs_bucket", "")
        if not actual_gcs_key_path:
            actual_gcs_key_path = gcs_settings.get("instagram_gcs_key_path", "")
        
        gcs_flag = gcs_settings.get("instagram_use_gcs", "1")
        if gcs_flag in ("1", "true", "True") and actual_gcs_bucket and actual_gcs_key_path and os.path.isfile(actual_gcs_key_path):
            use_gcs = True
    except Exception:
        pass

    # Step 0: Ensure video complies with Instagram Reel specifications (1080x1920, H.264, yuv420p)
    actual_video_path = video_path
    temp_transcoded = None
    gcs_blob = None

    try:
        try:
            ffprobe_bin = get_binary_path("ffprobe")
            probe_cmd = [
                ffprobe_bin, "-v", "error",
                "-show_entries", "stream=width,height,codec_name,pix_fmt:format=duration",
                "-of", "json", video_path
            ]
            probe_out = subprocess.check_output(probe_cmd, stderr=subprocess.DEVNULL)
            probe_json = json.loads(probe_out)
            streams = probe_json.get("streams", [{}])
            probe_data = streams[0] if streams else {}
            v_codec = probe_data.get("codec_name", "").lower()
            v_w = int(probe_data.get("width", 0))
            v_h = int(probe_data.get("height", 0))
            v_pix = probe_data.get("pix_fmt", "").lower()
            v_dur = float(probe_json.get("format", {}).get("duration", 0) or 0)

            needs_transcode = (v_codec != "h264" or v_w != 1080 or v_h != 1920 or "420" not in v_pix or v_dur > 900.0)

            if needs_transcode:
                reasons = []
                if v_dur > 900.0:
                    reasons.append(f"trimming {int(v_dur)}s to 15m (900s) Reel API limit")
                if v_codec != "h264" or v_w != 1080 or v_h != 1920 or "420" not in v_pix:
                    reasons.append(f"optimizing format ({v_codec} {v_w}x{v_h}) to standard 1080x1920 H.264")
                if log_callback:
                    log_callback(f"Optimizing video for Instagram Reel ({', '.join(reasons)})...")

                ffmpeg_bin = get_binary_path("ffmpeg")
                temp_transcoded = os.path.join(tempfile.gettempdir(), f"ig_reel_{int(time.time())}.mp4")
                vcodec_args = [
                    "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.0",
                    "-r", "30", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                    "-crf", "22", "-pix_fmt", "yuv420p", "-color_range", "tv",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"
                ]

                time_args = ["-ss", "0", "-t", "900"] if v_dur > 900.0 else []
                audio_args = ["-af", "afade=t=out:st=897:d=3", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"] if v_dur > 900.0 else ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]

                conv_cmd = [
                    ffmpeg_bin, "-y"
                ] + time_args + [
                    "-i", video_path,
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                ] + vcodec_args + audio_args + [
                    "-movflags", "+faststart",
                    temp_transcoded
                ]
                subprocess.run(conv_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if os.path.isfile(temp_transcoded):
                    actual_video_path = temp_transcoded
        except Exception as probe_err:
            if log_callback:
                log_callback(f"Format check note: using source video ({probe_err})")

        file_size = os.path.getsize(actual_video_path)

        # 1. Step 1: Initialize Media Container (via GCS Signed URL or Direct Resumable)
        init_url = f"https://graph.facebook.com/v26.0/{ig_user_id}/media"
        init_data = {
            "media_type": "REELS",
            "caption": caption[:2200],
            "share_to_feed": "true",
            "access_token": access_token
        }

        # If GCS enabled, upload to bucket and generate signed URL
        if use_gcs:
            try:
                from google.cloud import storage
                import datetime as dt_mod
                
                gcs_client = storage.Client.from_service_account_json(actual_gcs_key_path)
                bucket = gcs_client.bucket(actual_gcs_bucket)
                blob_name = f"temp_reels/reel_{int(time.time())}_{os.path.basename(actual_video_path)}"
                gcs_blob = bucket.blob(blob_name)

                if log_callback:
                    log_callback(f"Uploading optimized Reel to Google Cloud Storage ({file_size / (1024*1024):.1f} MB)...")

                gcs_blob.upload_from_filename(actual_video_path, content_type="video/mp4")

                signed_url = gcs_blob.generate_signed_url(
                    version="v4",
                    expiration=dt_mod.timedelta(hours=2),
                    method="GET"
                )

                init_data["video_url"] = signed_url
                if log_callback:
                    log_callback("Google Cloud temporary URL generated. Initializing Meta fast-path container...")
            except Exception as gcs_err:
                if log_callback:
                    log_callback(f"GCS upload fallback notice ({gcs_err}). Switching to direct streaming...")
                init_data["upload_type"] = "resumable"
        else:
            init_data["upload_type"] = "resumable"
            if log_callback:
                log_callback("Initializing Instagram Reel media container (direct streaming)...")

        # Note: Meta Graph API restricts 'scheduled_publish_time' on Reels containers to whitelisted partner apps.
        # Passing scheduled_publish_time causes error '(#3) User must be on whitelist'. Reels are published directly upon processing.
        if schedule_time_iso and log_callback:
            log_callback(f"Instagram Reel will be published upon processing completion (scheduled date: {schedule_time_iso}).")

        init_res = requests.post(init_url, data=init_data, timeout=30)
        init_json = init_res.json()
        if "error" in init_json:
            return "", f"Instagram Init Error: {init_json['error'].get('message')}"

        container_id = init_json.get("id")
        upload_uri = init_json.get("uri")

        if not container_id:
            return "", "Instagram API did not return a media container ID."

        if log_callback:
            log_callback(f"Instagram media container created (ID: {container_id})")

        # If upload_uri returned, stream video file directly via resumable protocol
        if upload_uri:
            if log_callback:
                log_callback(f"Uploading Reel video data ({file_size / (1024*1024):.1f} MB)...")

            import re
            curl_bin = get_binary_path("curl") or "curl"
            curl_cmd = [
                curl_bin, "-#",
                "-X", "POST", upload_uri,
                "-H", f"Authorization: OAuth {access_token}",
                "-H", "offset: 0",
                "-H", f"file_size: {file_size}",
                "--data-binary", f"@{actual_video_path}",
                "--write-out", "\n%{http_code}"
            ]

            proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            last_reported_pct = -1

            # Read stderr for curl progress bar
            while True:
                line = proc.stderr.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    match = re.search(r'(\d+(?:\.\d+)?)%', line)
                    if match:
                        pct = int(float(match.group(1)))
                        if pct != last_reported_pct and (pct % 5 == 0 or pct == 100):
                            last_reported_pct = pct
                            if log_callback:
                                log_callback(f"Uploading to Instagram: {pct}% complete...")

            stdout_data, _ = proc.communicate()
            stdout_lines = stdout_data.strip().rsplit("\n", 1)
            response_body = stdout_lines[0] if len(stdout_lines) > 1 else ""
            http_status = int(stdout_lines[-1]) if stdout_lines[-1].isdigit() else 0

            # Verify if upload succeeded (200 OK or container transitioned)
            upload_ok = (http_status == 200)
            if not upload_ok:
                # Check if container actually accepted the media stream despite edge proxy status
                try:
                    chk_res = requests.get(
                        f"https://graph.facebook.com/v26.0/{container_id}",
                        params={"fields": "status_code,status", "access_token": access_token},
                        timeout=15
                    )
                    chk_code = chk_res.json().get("status_code", "")
                    if chk_code in ("IN_PROGRESS", "FINISHED"):
                        upload_ok = True
                except Exception:
                    pass

            if not upload_ok:
                err_msg = f"HTTP {http_status}"
                try:
                    resp_json = json.loads(response_body)
                    err_msg = resp_json.get("message") or resp_json.get("error", {}).get("message") or err_msg
                except Exception:
                    pass
                return "", f"Instagram video upload failed: {err_msg}"

            if log_callback:
                log_callback("Video bytes transferred successfully. Checking processing status...")

        # 2. Step 2: Poll Container Processing Status
        time.sleep(10)  # Initial buffer for Meta ingestion

        status_url = f"https://graph.facebook.com/v26.0/{container_id}"
        status_params = {
            "fields": "status_code,status",
            "access_token": access_token
        }

        max_attempts = 60  # 60 checks * 15s = 900s (15 minutes max)
        status_code = ""
        rate_limit_hits = 0

        for attempt in range(max_attempts):
            try:
                st_res = requests.get(status_url, params=status_params, timeout=25)
                st_json = st_res.json()

                # Handle Meta API rate limit or error responses gracefully
                if "error" in st_json:
                    err_info = st_json["error"]
                    err_code = err_info.get("code", 0)
                    err_msg = err_info.get("message", "")

                    if err_code == 4 or "request limit" in err_msg.lower():
                        rate_limit_hits += 1
                        if rate_limit_hits > 6:
                            return "", "Meta hourly API quota reached. Please wait 15-30 minutes for Meta's hourly window to reset and click Deploy again."
                        if log_callback:
                            log_callback(f"Meta request limit active. Pausing 90s to allow hourly quota window to clear (pause {rate_limit_hits}/6)...")
                        time.sleep(90)
                        continue
                    else:
                        return "", f"Instagram Status Error: {err_msg}"

                status_code = st_json.get("status_code", "")

                if log_callback:
                    log_callback(f"Instagram processing status: {status_code} (check {attempt + 1}/{max_attempts})")

                if status_code == "FINISHED":
                    break
                elif status_code == "ERROR":
                    err_details = st_json.get("status", "Unknown processing error")
                    return "", f"Instagram video processing failed: {err_details}"
                elif status_code == "EXPIRED":
                    return "", "Instagram media container expired before publishing."
            except Exception as e:
                if log_callback:
                    log_callback(f"Status check warning: {e}")

            time.sleep(15)  # 15s between checks

        # DO NOT attempt to publish if processing has not finished
        if status_code != "FINISHED":
            return "", f"Instagram video processing timed out after {max_attempts * 15}s (Last status: {status_code or 'Unknown'}). Please retry deployment."

        # Brief grace period after FINISHED to ensure Meta's distributed edge servers propagate container readiness
        time.sleep(5)

        # 3. Step 3: Publish Media Container Immediately
        if log_callback:
            log_callback("Publishing Instagram Reel to feed...")

        publish_url = f"https://graph.facebook.com/v26.0/{ig_user_id}/media_publish"
        pub_data = {
            "creation_id": container_id,
            "access_token": access_token
        }

        media_id = None
        for pub_attempt in range(6):
            pub_res = requests.post(publish_url, data=pub_data, timeout=30)
            pub_json = pub_res.json()
            if "error" not in pub_json:
                media_id = pub_json.get("id")
                break

            err_obj = pub_json.get("error", {})
            err_msg = err_obj.get("message", "Unknown error")
            err_subcode = err_obj.get("error_subcode", 0)

            # "Media ID is not available" (error subcode 2207027) means Meta's edge servers are still finalizing/propagating the container
            if ("Media ID is not available" in err_msg or "not ready" in err_msg.lower() or err_subcode == 2207027) and pub_attempt < 5:
                if log_callback:
                    log_callback(f"Media container synchronizing on Meta servers... retrying publish in 10s (attempt {pub_attempt + 1}/6)")
                time.sleep(10)
            else:
                return "", f"Instagram Publish Error: {err_msg}"

        if not media_id:
            return "", "Instagram API did not return published media ID."

        if log_callback:
            log_callback(f"Instagram Reel published successfully! Media ID: {media_id}")

        return media_id, ""
    except Exception as e:
        return "", f"Unexpected error during Instagram Reel upload: {e}"
    finally:
        if gcs_blob:
            try:
                gcs_blob.delete()
                if log_callback:
                    log_callback("Cleaned up temporary video from Google Cloud Storage.")
            except Exception:
                pass
        if temp_transcoded and os.path.isfile(temp_transcoded):
            try:
                os.remove(temp_transcoded)
            except Exception:
                pass

