import json
import os
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Flask


PLACE_ID = 13358463560

RIFT_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
BOSS_WEBHOOK_URL = os.getenv("BOSS_WEBHOOK_URL", "").strip()

CHECK_DELAY = 180
RIFT_WINDOW = 5 * 60
PAGE_DELAY = 1.0
RATE_LIMIT_DELAY = 1800
DISCORD_RATE_LIMIT_FALLBACK = 1800

TRACK_EXISTING_SERVERS_AS_NEW = False

SCRIPT_DIR = Path(__file__).resolve().parent
DB_FILE = SCRIPT_DIR / "servers.json"
BACKUP_DB_FILE = SCRIPT_DIR / "servers_backup.json"

TRACKERS = [
    {
        "key": "rift",
        "label": "Rift",
        "interval": int(os.getenv("RIFT_INTERVAL", str(90 * 60))),
        "webhook_url": RIFT_WEBHOOK_URL,
        "content": "@everyone \u26a1 Rift Incoming!",
        "footer": "Rift Tracker Bot",
    },
    {
        "key": "boss",
        "label": "Bosses",
        "interval": int(os.getenv("BOSS_INTERVAL", str(120 * 60))),
        "webhook_url": BOSS_WEBHOOK_URL,
        "content": "@everyone \u26a1 Bosses Incoming!",
        "footer": "Boss Tracker Bot",
    },
]

app = Flask(__name__)
discord_blocked_until = {}
bot_status = {
    "started": False,
    "last_check": "never",
    "last_error": "",
    "checked_servers": 0,
    "tracked_new_servers": 0,
    "baseline_servers": 0,
    "trackers": {},
}


@app.get("/")
def health_check():
    return "OK - tracker bot is running\n", 200


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
        log(f"[DB] Permission denied for {DB_FILE}. Using {BACKUP_DB_FILE}.")
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


def send_webhook_payload(tracker, payload):
    webhook_url = tracker["webhook_url"]
    tracker_key = tracker["key"]

    if not webhook_url:
        log(f"[{tracker_key.upper()}] Webhook is empty. Message was not sent.")
        return False

    now = time.time()
    blocked_until = discord_blocked_until.get(tracker_key, 0)
    if now < blocked_until:
        wait_for = int(blocked_until - now)
        log(f"[{tracker_key.upper()}] Discord rate limited. Skipping send for {wait_for}s.")
        return False

    try:
        response = requests.post(webhook_url, json=payload, timeout=20)
    except requests.RequestException as error:
        log(f"[{tracker_key.upper()}] Discord request failed: {error}")
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

        discord_blocked_until[tracker_key] = time.time() + retry_after
        log(f"[{tracker_key.upper()}] Discord rate limited. Waiting {int(retry_after)}s.")
        return False

    log(f"[{tracker_key.upper()}] Discord error {response.status_code}: {response.text[:300]}")
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
        "notified_cycles": {},
    }


def normalize_record(record, now):
    if isinstance(record, dict):
        record.setdefault("first_seen", now)
        record.setdefault("baseline", False)
        record.setdefault("notified_cycles", {})

        # Migrate the old single-tracker field without losing existing state.
        if "last_notified_cycle" in record and "rift" not in record["notified_cycles"]:
            record["notified_cycles"]["rift"] = record.get("last_notified_cycle", -1)

        return record

    if isinstance(record, (int, float)):
        return {
            "first_seen": float(record),
            "baseline": False,
            "notified_cycles": {},
        }

    return make_record(now)


def build_tracker_embed(tracker, server_id, seconds_to_event, age, players, target_epoch):
    label = tracker["label"]
    roblox_link = f"https://www.roblox.com/games/{PLACE_ID}?gameId={server_id}"
    roblox_protocol = f"roblox://placeId={PLACE_ID}&gameInstanceId={server_id}"
    target_time = format_time(age + seconds_to_event)

    return {
        "title": "Server Found!",
        "description": f"This server is the closest tracked {label} window.",
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
                "name": "Countdown",
                "value": f"<t:{target_epoch}:R>",
                "inline": True,
            },
            {
                "name": "Exact Time",
                "value": f"<t:{target_epoch}:T>",
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
            "text": tracker["footer"],
        },
    }


def tracker_candidates_for_server(tracker, server_id, age, players, now):
    interval = tracker["interval"]
    cycle = int(age // interval)
    phase = age % interval
    seconds_to_event = int(interval - phase)

    nearest = (seconds_to_event, server_id, int(age), players)

    if 0 < seconds_to_event <= RIFT_WINDOW:
        notify_cycle = cycle + 1
        target_epoch = int(now + seconds_to_event)
        candidate = (server_id, seconds_to_event, int(age), notify_cycle, players, target_epoch)
        return nearest, candidate

    return nearest, None


def run_bot():
    bot_status["started"] = True

    if not any(tracker["webhook_url"] for tracker in TRACKERS):
        bot_status["last_error"] = "No webhook env vars are set"
        log("Set DISCORD_WEBHOOK_URL and/or BOSS_WEBHOOK_URL before starting.")
        return

    db = load_db()
    first_scan = not bool(db)

    log("Combined tracker bot started.")
    log("Tracking Roblox servers once for Rift and Bosses...")

    while True:
        try:
            now = time.time()
            servers = get_servers()
            active_ids = set()
            tracked_new_servers = 0
            baseline_servers = 0
            tracker_state = {
                tracker["key"]: {
                    "candidates": [],
                    "nearest": [],
                    "sent": 0,
                }
                for tracker in TRACKERS
            }

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

                for tracker in TRACKERS:
                    state = tracker_state[tracker["key"]]
                    nearest, candidate = tracker_candidates_for_server(tracker, server_id, age, players, now)
                    state["nearest"].append(nearest)
                    if candidate:
                        state["candidates"].append(candidate)

            removed = 0
            for server_id in list(db):
                if server_id not in active_ids:
                    del db[server_id]
                    removed += 1

            for tracker in TRACKERS:
                tracker_key = tracker["key"]
                state = tracker_state[tracker_key]
                state["candidates"].sort(key=lambda item: item[1])
                state["nearest"].sort(key=lambda item: item[0])

                for server_id, seconds_to_event, age, notify_cycle, players, target_epoch in state["candidates"][:1]:
                    record = normalize_record(db[server_id], now)
                    notified_cycles = record.setdefault("notified_cycles", {})
                    if notified_cycles.get(tracker_key) == notify_cycle:
                        continue

                    embed = build_tracker_embed(tracker, server_id, seconds_to_event, age, players, target_epoch)
                    payload = {
                        "content": tracker["content"],
                        "allowed_mentions": {"parse": ["everyone"]},
                        "embeds": [embed],
                    }

                    if send_webhook_payload(tracker, payload):
                        notified_cycles[tracker_key] = notify_cycle
                        db[server_id] = record
                        state["sent"] += 1
                        log(f"[{tracker_key.upper()} SENT] {server_id}")

            save_db(db)
            first_scan = False

            bot_status["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            bot_status["last_error"] = ""
            bot_status["checked_servers"] = len(active_ids)
            bot_status["tracked_new_servers"] = tracked_new_servers
            bot_status["baseline_servers"] = baseline_servers
            bot_status["trackers"] = {}

            log(
                f"Checked: {len(active_ids)} servers. "
                f"Tracked: {tracked_new_servers}. Baseline: {baseline_servers}. Removed: {removed}."
            )

            for tracker in TRACKERS:
                tracker_key = tracker["key"]
                state = tracker_state[tracker_key]
                status = {
                    "candidates": len(state["candidates"]),
                    "sent": state["sent"],
                    "nearest": "",
                    "nearest_server_id": "",
                }

                if state["nearest"]:
                    seconds_to_event, server_id, age, players = state["nearest"][0]
                    status["nearest"] = format_time(seconds_to_event)
                    status["nearest_server_id"] = server_id
                    log(
                        f"[{tracker_key.upper()}] Candidates: {len(state['candidates'])}. "
                        f"Sent: {state['sent']}. Nearest: {format_time(seconds_to_event)} | "
                        f"Uptime: {format_time(age)} | Players: {players} | {server_id}"
                    )
                else:
                    log(f"[{tracker_key.upper()}] No tracked non-baseline servers yet.")

                bot_status["trackers"][tracker_key] = status

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
