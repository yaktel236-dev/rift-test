import json
import os
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Flask


PLACE_ID = 13358463560

# Discord webhook:
# 1) Better: set DISCORD_WEBHOOK_URL in PowerShell.
# 2) Easier: paste your new webhook between the quotes below.
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

RIFT_INTERVAL = 90 * 60
CHECK_DELAY = 180
RIFT_WINDOW = 5 * 60
RIFT_GRACE_AFTER = 10 * 60
PAGE_DELAY = 1.0
RATE_LIMIT_DELAY = 300

# If False, servers found on the first scan are saved as baseline only.
# This avoids fake "new server" times for servers that existed before bot start.
TRACK_EXISTING_SERVERS_AS_NEW = False

SCRIPT_DIR = Path(__file__).resolve().parent
DB_FILE = SCRIPT_DIR / "servers.json"
BACKUP_DB_FILE = SCRIPT_DIR / "servers_backup.json"

app = Flask(__name__)
bot_started = False


@app.get("/")
def health_check():
    return "OK - rift bot is running\n", 200


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_db():
    db_file = DB_FILE if DB_FILE.exists() else BACKUP_DB_FILE
    if not db_file.exists():
        return {}

    try:
        with db_file.open("r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_db(data):
    try:
        with DB_FILE.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except PermissionError:
        print(f"[DB] Permission denied for {DB_FILE}. Using {BACKUP_DB_FILE}.")
        with BACKUP_DB_FILE.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)


def get_servers():
    servers = []
    cursor = None

    while True:
        params = {
            "limit": 100,
            "sortOrder": "Asc",
        }
        if cursor:
            params["cursor"] = cursor

        url = f"https://games.roblox.com/v1/games/{PLACE_ID}/servers/Public"
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()

        servers.extend(data.get("data", []))

        cursor = data.get("nextPageCursor")
        if not cursor:
            break

        time.sleep(PAGE_DELAY)

    return servers


def send_webhook(content):
