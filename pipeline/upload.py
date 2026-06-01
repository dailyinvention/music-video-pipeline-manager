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

SCOPES = ["https://www.googleapis.com/auth/youtube"]


# ---------------------------------------------------------------------------
# Simple Template Renderer (Handlebars-style)
# ---------------------------------------------------------------------------

def render_template(template_str: str, body_text: str, project_title: str) -> str:
    """
    Renders template string replacing placeholders with context variables:
    {{body}}, {{title}}, {{year}}, {{month}}, {{day}}, {{date}}
    """
    now = datetime.datetime.now()
    context = {
        "body": body_text or "",
        "title": project_title or "",
        "year": str(now.year),
        "month": now.strftime("%B"), # Month name (e.g. "May")
        "day": str(now.day),
        "date": now.strftime("%Y-%m-%d"),
    }
    
    rendered = template_str or "{{body}}"
    for key, val in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", val)
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
        
        status = {
            "privacyStatus": "private",  # Must be private to support schedule_time
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
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                if log_callback:
                    log_callback(f"Uploading to YouTube: {progress}% complete...")
                    
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
        
        status = {}
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
