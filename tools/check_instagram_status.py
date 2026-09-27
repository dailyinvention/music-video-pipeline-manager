#!/usr/bin/env python3
"""
Quick utility to check the ingestion / processing status of an Instagram media container.
Usage:
    python tools/check_instagram_status.py [CONTAINER_ID]
"""
import sys
import sqlite3
import requests

def main():
    container_id = sys.argv[1] if len(sys.argv) > 1 else "18117242524973613"

    conn = sqlite3.connect("pipeline_data/pipeline.db")
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings WHERE key IN ('instagram_access_token', 'instagram_account_id')")
    settings = dict(cursor.fetchall())
    conn.close()

    token = settings.get("instagram_access_token", "")
    if not token:
        print("Error: No Instagram access token found in database.")
        sys.exit(1)

    url = f"https://graph.facebook.com/v26.0/{container_id}"
    params = {
        "fields": "status_code,status",
        "access_token": token
    }

    try:
        res = requests.get(url, params=params, timeout=15)
        data = res.json()

        print(f"\nInstagram Container: {container_id}")
        if "error" in data:
            print(f"❌ Error: {data['error'].get('message')}")
        else:
            status_code = data.get("status_code", "UNKNOWN")
            status_msg = data.get("status", "")
            print(f"Status Code : {status_code}")
            print(f"Details     : {status_msg}")
            
            if status_code == "FINISHED":
                print("\n✅ Media is fully processed and ready to publish!")
            elif status_code == "IN_PROGRESS":
                print("\n⏳ Media is still being processed on Meta's servers.")
            elif status_code == "ERROR":
                print("\n❌ Processing failed on Meta's servers.")
    except Exception as e:
        print(f"Failed to query Meta Graph API: {e}")

if __name__ == "__main__":
    main()
