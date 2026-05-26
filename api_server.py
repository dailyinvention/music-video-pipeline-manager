#!/usr/bin/env python3
"""
REST API server for the Music Video Pipeline Manager.

Allows remote clients or scripts to send videos and audio files, configure
processing parameters, and queue them directly into the processing pipeline.

Run with:
    python api_server.py
"""

import os
import sys
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify

# Add project root to sys.path to import the pipeline module
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import db
from pipeline.pipeline import STEP_NAMES

app = Flask(__name__)

DB_PATH = os.path.join(ROOT, "pipeline_data", "pipeline.db")


@app.route("/api/projects", methods=["POST"])
def upload_project():
    """
    Accepts multipart form-data to create a new project.
    
    Parameters:
        name (str): Unique project name
        video_file (file): YouTube Long Video
        audio_file (file): YouTube Long Audio
        facebook_video (file, optional): Facebook Short Video
        facebook_audio (file, optional): Facebook Short Audio
        overlay_text (str, optional): Title overlay text for shorts
        font_size (int, optional): Title font size (default: 90)
        loop_pick (int, optional): Selected loop candidate index (default: 1)
        loop_fade (float, optional): Loop crossfade duration (default: 2.0)
        process_facebook (int, optional): 0 or 1 (default: 1)
        process_youtube (int, optional): 0 or 1 (default: 1)
        skip_shorts (int, optional): 0 or 1 (default: 0)
        queue (str, optional): 'true' or 'false' (default: 'true')
    """
    try:
        conn = db.get_connection(DB_PATH)
        
        # 1. Resolve watch folder for file uploads
        watch_folder = db.get_setting(conn, "watch_folder", "").strip()
        if not watch_folder or watch_folder == "Not set" or not os.path.isdir(watch_folder):
            watch_folder = os.path.join(ROOT, "projects")
            os.makedirs(watch_folder, exist_ok=True)
            
        name = request.form.get("name")
        if not name:
            conn.close()
            return jsonify({"error": "Project 'name' parameter is required."}), 400
            
        # Create safe folder name
        safe_name = "".join([c if c.isalnum() or c in " -_" else "_" for c in name]).strip()
        project_dir = os.path.join(watch_folder, safe_name)
        
        # Handle directory name collisions gracefully
        if os.path.exists(project_dir):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            project_dir = f"{project_dir}_{timestamp}"
            
        os.makedirs(project_dir, exist_ok=True)
        
        # 2. Save incoming files
        saved_files = {}
        for key in ["video_file", "audio_file", "facebook_video", "facebook_audio"]:
            if key in request.files:
                file_obj = request.files[key]
                if file_obj and file_obj.filename:
                    filename = secure_filename(file_obj.filename)
                    dest_path = os.path.join(project_dir, filename)
                    file_obj.save(dest_path)
                    saved_files[key] = dest_path

        # 3. Read form settings
        skip_shorts = int(request.form.get("skip_shorts", 0))
        process_facebook = int(request.form.get("process_facebook", 1))
        process_youtube = int(request.form.get("process_youtube", 1))
        overlay_text = request.form.get("overlay_text", "")
        font_size = int(request.form.get("font_size", 90))
        loop_pick = int(request.form.get("loop_pick", 1))
        loop_fade = float(request.form.get("loop_fade", 2.0))
        
        # 4. Insert project
        pid = db.create_project(
            conn,
            folder_path=project_dir,
            name=name,
            video_file=saved_files.get("video_file", ""),
            audio_file=saved_files.get("audio_file", ""),
            facebook_video=saved_files.get("facebook_video", ""),
            youtube_video=saved_files.get("video_file", ""),
            facebook_audio=saved_files.get("facebook_audio", ""),
            skip_shorts=skip_shorts,
            process_facebook=process_facebook,
            process_youtube=process_youtube
        )
        
        # Update settings
        db.update_project(
            conn, pid,
            overlay_text=overlay_text,
            font_size=font_size,
            loop_pick=loop_pick,
            loop_fade=loop_fade
        )
        
        # 5. Automatically Queue if requested
        queue_now = request.form.get("queue", "true").lower() == "true"
        if queue_now:
            db.update_project(
                conn, pid,
                status="queued",
                current_step=0,
                queued_at=datetime.now().isoformat(),
                error_message="",
            )
            # Populate initial pending steps logs
            conn.execute("DELETE FROM process_log WHERE project_id = ?", (pid,))
            for step_num, step_name in STEP_NAMES.items():
                db.log_step(conn, pid, step_num, step_name, "pending")
            conn.commit()

        conn.close()
        
        return jsonify({
            "status": "success",
            "project_id": pid,
            "folder_path": project_dir,
            "state": "queued" if queue_now else "staged",
            "saved_files": saved_files
        }), 201

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/projects", methods=["GET"])
def list_projects():
    """List status of all projects in the system."""
    try:
        conn = db.get_connection(DB_PATH)
        projects = db.get_all_projects(conn)
        conn.close()
        return jsonify([dict(p) for p in projects])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/projects/<int:pid>", methods=["GET"])
def get_project(pid):
    """Retrieve details and execution steps for a project."""
    try:
        conn = db.get_connection(DB_PATH)
        project = db.get_project(conn, pid)
        if not project:
            conn.close()
            return jsonify({"error": f"Project ID {pid} not found"}), 404
            
        steps = db.get_steps(conn, pid)
        conn.close()
        
        data = dict(project)
        data["steps"] = [dict(s) for s in steps]
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    # Expose to local network on port 5000
    app.run(host="0.0.0.0", port=5000, debug=True)
