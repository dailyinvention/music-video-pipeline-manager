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
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow

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


def render_template(template_str: str, body_text: str, project_title: str, yt_url: str = "", overlay_text: str = "") -> str:
    """
    Renders template string replacing placeholders with context variables:
    {{body}}, {{quote}}, {{title}}, {{youtube-url}}, {{youtube_url}}, {{overlay_text}}, {{year}}, {{month}}, {{day}}, {{date}}
    """
    now = datetime.datetime.now()
    clean_q = extract_quote(body_text)

    context = {
        "body": clean_q or (body_text or ""),
        "quote": clean_q or (body_text or ""),
        "title": project_title or "",
        "overlay_text": overlay_text or "",
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
    base_url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
    
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
        url = f"https://graph.facebook.com/v19.0/{page_id}/videos"
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
        url = f"https://graph.facebook.com/v19.0/{page_id}/scheduled_posts"
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
        page_url = f"https://graph.facebook.com/v19.0/{page_id}"
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
        url = "https://graph.facebook.com/v19.0/oauth/access_token"
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
        f"https://www.facebook.com/v19.0/dialog/oauth"
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
        token_url = "https://graph.facebook.com/v19.0/oauth/access_token"
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
    rendered_desc = render_template(template, project.get("fb_post_body") or "", project.get("name") or "")
    rendered_desc = rendered_desc.replace("{{youtube-url}}", yt_url)
    rendered_desc = rendered_desc.replace("{{overlay_text}}", project.get("overlay_text") or "")
    while rendered_desc.startswith('""'):
        rendered_desc = rendered_desc[1:]

    if log_callback:
        log_callback(f"Syncing Facebook post description with YouTube URL ({yt_url})...")

    # 1. Update Video Object Description
    url_vid = f"https://graph.facebook.com/v19.0/{fb_vid_id}"
    res_vid = requests.post(url_vid, data={"access_token": page_token, "description": rendered_desc}, timeout=30).json()

    # 2. Get Feed Post ID & Update Timeline Feed Post message
    post_updated = False
    try:
        get_resp = requests.get(url_vid, params={"fields": "post_id", "access_token": page_token}, timeout=15).json()
        post_id = get_resp.get("post_id")
        if post_id:
            full_post_id = f"{page_id}_{post_id}" if "_" not in str(post_id) and page_id else str(post_id)
            url_post = f"https://graph.facebook.com/v19.0/{full_post_id}"
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
        rendered_desc = render_template(template, body_input, project.get("name") or "", yt_url=main_yt_url, overlay_text=project.get("overlay_text") or "")

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

        comment_text = render_template(tpl, "", project.get("name") or "", yt_url=main_yt_url, overlay_text=project.get("overlay_text") or "")

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

    # PKCE Generation
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")

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
    privacy_level: str = "PUBLIC_TO_EVERYONE",
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
        log_callback(f"Initializing TikTok Direct Post upload ({file_size / (1024 * 1024):.1f} MB)...")

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

    payload = {
        "post_info": post_info,
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,
            "total_chunk_count": 1
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
                if put_res.status_code not in (200, 201, 204):
                    return "", f"TikTok video upload failed (HTTP {put_res.status_code}): {put_res.text}"
            else:
                uploaded_bytes = 0
                for i in range(total_chunk_count):
                    start_byte = i * chunk_size
                    end_byte = min(start_byte + chunk_size, file_size) - 1
                    current_chunk_len = end_byte - start_byte + 1
                    chunk_data = f.read(current_chunk_len)
                    
                    upload_headers = {
                        "Content-Type": "video/mp4",
                        "Content-Length": str(current_chunk_len),
                        "Content-Range": f"bytes {start_byte}-{end_byte}/{file_size}"
                    }
                    put_res = requests.put(upload_url, headers=upload_headers, data=chunk_data, timeout=300)
                    if put_res.status_code not in (200, 201, 204):
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
