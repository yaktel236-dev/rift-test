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
    if not WEBHOOK_URL:
        print("[DISCORD] Webhook is empty. Message was not sent.")
        return False

    response = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)

    if response.status_code in (200, 204):
        return True

    print(f"[DISCORD] Error {response.status_code}: {response.text[:300]}")
    return False


def format_time(seconds):
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def make_record(now, baseline=False):
    return {
        "first_seen": now,
        "baseline": baseline,
        "last_notified_cycle": -1,
    }


def normalize_record(record, now):
    if isinstance(record, dict):
        record.setdefault("first_seen", now)
        record.setdefault("baseline", False)
        record.setdefault("last_notified_cycle", -1)
        return record

    if isinstance(record, (int, float)):
        return {
            "first_seen": float(record),
            "baseline": False,
            "last_notified_cycle": -1,
        }

    return make_record(now)


def run_bot():
    if not WEBHOOK_URL:
        print("Set DISCORD_WEBHOOK_URL before starting, or paste webhook in WEBHOOK_URL.")
        return

    db = load_db()
    first_scan = not bool(db)

    print("Bot started. Checking Discord webhook...")
    if send_webhook("Rift bot started. Discord webhook works."):
        print("[DISCORD] Test message sent.")
    else:
        print("[DISCORD] Test message failed. Check webhook URL.")

    print("Tracking Roblox servers...")

    while True:
        try:
            now = time.time()
            servers = get_servers()
            active_ids = set()
            candidates = []
            nearest = []

            for server in servers:
                server_id = server.get("id")
                players = int(server.get("playing") or 0)

                if not server_id or players < 1:
                    continue

                active_ids.add(server_id)

                if server_id not in db:
                    is_baseline = first_scan and not TRACK_EXISTING_SERVERS_AS_NEW
                    db[server_id] = make_record(now, baseline=is_baseline)

                    if is_baseline:
                        print(f"[BASELINE] Existing server saved: {server_id}")
                    else:
                        print(f"[NEW] Server found: {server_id}")

                record = normalize_record(db[server_id], now)
                db[server_id] = record

                if record.get("baseline"):
                    continue

                age = now - float(record["first_seen"])
                cycle = int(age // RIFT_INTERVAL)
                phase = age % RIFT_INTERVAL
                seconds_to_rift = int(RIFT_INTERVAL - phase)
                seconds_after_rift = int(phase)

                nearest.append((seconds_to_rift, server_id, int(age), players))

                if 0 < seconds_to_rift <= RIFT_WINDOW:
                    candidates.append(
                        (server_id, seconds_to_rift, int(age), cycle + 1, players, "incoming")
                    )
                elif age >= RIFT_INTERVAL and seconds_after_rift <= RIFT_GRACE_AFTER:
                    candidates.append(
                        (server_id, seconds_after_rift, int(age), cycle, players, "now")
                    )

            removed = 0
            for server_id in list(db):
                if server_id not in active_ids:
                    del db[server_id]
                    removed += 1

            candidates.sort(key=lambda item: item[1])
            nearest.sort(key=lambda item: item[0])

            sent = 0
            for server_id, rift_time, age, notify_cycle, players, mode in candidates[:5]:
                record = normalize_record(db[server_id], now)
                if record.get("last_notified_cycle") == notify_cycle:
                    continue

                roblox_link = f"https://www.roblox.com/games/{PLACE_ID}?gameId={server_id}"
                direct_join_link = f"https://l.rlnk.app/g/{PLACE_ID}/{server_id}"
                if mode == "incoming":
                    title = "Rift incoming!"
                    timing = f"Rift in: {format_time(rift_time)}"
                else:
                    title = "Rift should be active now!"
                    timing = f"Rift started about: {format_time(rift_time)} ago"

                message = (
                    f"{title}\n\n"
                    f"Uptime from first bot sighting: {format_time(age)}\n"
                    f"{timing}\n"
                    f"Players: {players}\n"
                    f"Direct join: {direct_join_link}\n"
                    f"Roblox page: {roblox_link}\n"
                    f"Server ID: {server_id}"
                )

                if send_webhook(message):
                    record["last_notified_cycle"] = notify_cycle
                    db[server_id] = record
                    sent += 1
                    print(f"[SENT] {server_id}")

            save_db(db)
            first_scan = False

            print(
                f"Checked: {len(active_ids)} servers. "
                f"Candidates: {len(candidates)}. Sent: {sent}. Removed: {removed}."
            )
            if nearest:
                seconds_to_rift, server_id, age, players = nearest[0]
                print(
                    f"Nearest tracked rift: {format_time(seconds_to_rift)} | "
                    f"Uptime: {format_time(age)} | Players: {players} | {server_id}"
                )
            else:
                print("No tracked non-baseline servers yet.")
            time.sleep(CHECK_DELAY)

        except requests.HTTPError as error:
            status_code = getattr(error.response, "status_code", None)
            if status_code == 429:
                print(f"[ROBLOX] Rate limit. Waiting {RATE_LIMIT_DELAY} seconds.")
                time.sleep(RATE_LIMIT_DELAY)
            else:
                print(f"[ROBLOX] HTTP error: {error}")
                time.sleep(30)
        except requests.RequestException as error:
            print(f"[NET] Network error: {error}")
            time.sleep(30)
        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as error:
            print(f"[ERROR] {error}")
            time.sleep(10)


if __name__ == "__main__":
    configure_console()
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
