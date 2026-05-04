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
CHECK_DELAY = 600
RIFT_WINDOW = 5 * 60
RIFT_GRACE_AFTER = 30 * 60
PAGE_DELAY = 3.0
RATE_LIMIT_DELAY = 1800
DISCORD_RATE_LIMIT_FALLBACK = 1800
MAX_RIFTS_PER_DISCORD_MESSAGE = 8

# If False, servers found on the first scan are saved as baseline only.
# This avoids fake "new server" times for servers that existed before bot start.
TRACK_EXISTING_SERVERS_AS_NEW = False

SCRIPT_DIR = Path(__file__).resolve().parent
DB_FILE = SCRIPT_DIR / "servers.json"
BACKUP_DB_FILE = SCRIPT_DIR / "servers_backup.json"

app = Flask(__name__)
discord_blocked_until = 0
bot_status = {
    "started": False,
    "last_check": "never",
    "last_error": "",
    "checked_servers": 0,
    "tracked_new_servers": 0,
    "baseline_servers": 0,
    "candidates": 0,
    "sent": 0,
    "nearest_rift": "",
    "nearest_server_id": "",
}


@app.get("/")
def health_check():
    return "OK - rift bot is running\n", 200


@app.get("/status")
def status_check():
    return bot_status, 200


def log(message):
    print(message, flush=True)


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


def send_webhook_payload(payload):
    global discord_blocked_until

    if not WEBHOOK_URL:
        log("[DISCORD] Webhook is empty. Message was not sent.")
        return False

    now = time.time()
    if now < discord_blocked_until:
        wait_for = int(discord_blocked_until - now)
        log(f"[DISCORD] Rate limited. Skipping send for {wait_for}s.")
        return False

    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    except requests.RequestException as error:
        log(f"[DISCORD] Request failed: {error}")
        return False

    if response.status_code in (200, 204):
        return True

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            retry_after = float(retry_after) if retry_after else float(response.json().get("retry_after", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            retry_after = 0

        if retry_after <= 0:
            retry_after = DISCORD_RATE_LIMIT_FALLBACK

        discord_blocked_until = time.time() + retry_after
        log(f"[DISCORD] Rate limited. Waiting {int(retry_after)}s before next send.")
        return False

    log(f"[DISCORD] Error {response.status_code}: {response.text[:300]}")
    return False


def send_webhook(content):
    return send_webhook_payload({"content": content})


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


def build_rift_embed(server_id, rift_time, age, players, mode):
    roblox_link = f"https://www.roblox.com/games/{PLACE_ID}?gameId={server_id}"
    roblox_protocol = f"roblox://placeId={PLACE_ID}&gameInstanceId={server_id}"

    if mode == "incoming":
        target_time = format_time(rift_time)
        description = "This server hit a Rift time window."
    else:
        target_time = f"active, started {format_time(rift_time)} ago"
        description = "This Rift should be active right now."

    return {
        "title": "Server Found!",
        "description": description,
        "color": 5763719,
        "fields": [
            {
                "name": "Uptime",
                "value": f"`{format_time(age)}`",
                "inline": True,
            },
            {
                "name": "Target Time",
                "value": f"`{target_time}`",
                "inline": True,
            },
            {
                "name": "Players",
                "value": f"`{players}`",
                "inline": True,
            },
            {
                "name": "Direct Join",
                "value": f"`{roblox_protocol}`",
                "inline": False,
            },
            {
                "name": "Backup Link",
                "value": f"[Open Roblox page]({roblox_link})",
                "inline": False,
            },
            {
                "name": "Job ID",
                "value": f"`{server_id}`",
                "inline": False,
            },
        ],
        "footer": {
            "text": "Rift Tracker Bot",
        },
    }


def run_bot():
    bot_status["started"] = True

    if not WEBHOOK_URL:
        bot_status["last_error"] = "DISCORD_WEBHOOK_URL is empty"
        log("Set DISCORD_WEBHOOK_URL before starting.")
        return

    db = load_db()
    first_scan = not bool(db)

    log("Bot started.")
    log("Tracking Roblox servers...")

    while True:
        try:
            now = time.time()
            servers = get_servers()
            active_ids = set()
            candidates = []
            nearest = []
            tracked_new_servers = 0
            baseline_servers = 0

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
                        log(f"[BASELINE] Existing server saved: {server_id}")
                    else:
                        log(f"[NEW] Server found: {server_id}")

                record = normalize_record(db[server_id], now)
                db[server_id] = record

                if record.get("baseline"):
                    baseline_servers += 1
                    continue

                tracked_new_servers += 1
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
            notification_items = []
            embeds = []

            for server_id, rift_time, age, notify_cycle, players, mode in candidates[:MAX_RIFTS_PER_DISCORD_MESSAGE]:
                record = normalize_record(db[server_id], now)
                if record.get("last_notified_cycle") == notify_cycle:
                    continue

                embeds.append(build_rift_embed(server_id, rift_time, age, players, mode))
                notification_items.append((server_id, notify_cycle))

            if embeds:
                payload = {
                    "content": "@everyone ⚡ Rift Incoming!",
                    "allowed_mentions": {"parse": ["everyone"]},
                    "embeds": embeds,
                }

                if send_webhook_payload(payload):
                    for server_id, notify_cycle in notification_items:
                        record = normalize_record(db[server_id], now)
                        record["last_notified_cycle"] = notify_cycle
                        db[server_id] = record
                        sent += 1
                        log(f"[SENT] {server_id}")

            save_db(db)
            first_scan = False

            bot_status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            bot_status["last_error"] = ""
            bot_status["checked_servers"] = len(active_ids)
            bot_status["tracked_new_servers"] = tracked_new_servers
            bot_status["baseline_servers"] = baseline_servers
            bot_status["candidates"] = len(candidates)
            bot_status["sent"] = sent

            log(
                f"Checked: {len(active_ids)} servers. "
                f"Candidates: {len(candidates)}. Sent: {sent}. Removed: {removed}."
            )
            if nearest:
                seconds_to_rift, server_id, age, players = nearest[0]
                bot_status["nearest_rift"] = format_time(seconds_to_rift)
                bot_status["nearest_server_id"] = server_id
                log(
                    f"Nearest tracked rift: {format_time(seconds_to_rift)} | "
                    f"Uptime: {format_time(age)} | Players: {players} | {server_id}"
                )
            else:
                bot_status["nearest_rift"] = ""
                bot_status["nearest_server_id"] = ""
                log("No tracked non-baseline servers yet.")
            time.sleep(CHECK_DELAY)

        except requests.HTTPError as error:
            status_code = getattr(error.response, "status_code", None)
            if status_code == 429:
                bot_status["last_error"] = "Roblox rate limit"
                log(f"[ROBLOX] Rate limit. Waiting {RATE_LIMIT_DELAY} seconds.")
                time.sleep(RATE_LIMIT_DELAY)
            else:
                bot_status["last_error"] = str(error)
                log(f"[ROBLOX] HTTP error: {error}")
                time.sleep(30)
        except requests.RequestException as error:
            bot_status["last_error"] = str(error)
            log(f"[NET] Network error: {error}")
            time.sleep(30)
        except KeyboardInterrupt:
            log("Bot stopped.")
            break
        except Exception as error:
            bot_status["last_error"] = str(error)
            log(f"[ERROR] {error}")
            time.sleep(10)


if __name__ == "__main__":
    configure_console()
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
