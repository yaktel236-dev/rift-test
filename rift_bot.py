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
