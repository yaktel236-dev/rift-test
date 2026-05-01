
    while True:
        params = {
            "limit": 100,
            "sortOrder": "Asc",
        }
        if cursor:
            params["cursor"] = cursor

        url = f"https://games.roblox.com/v1/games/{PLACE_ID}/servers/Public"
        data = request_json(url, params=params)

        servers.extend(data.get("data", []))

        cursor = data.get("nextPageCursor")
        if not cursor:
            break

        time.sleep(0.2)

    return servers


def send_webhook(content):
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL не задан, сообщение не отправлено.")
        return

    response = requests.post(WEBHOOK_URL, json={"content": content}, timeout=20)
    response.raise_for_status()


def format_time(seconds):
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}ч {minutes}м {secs}с"
    return f"{minutes}м {secs}с"


def ensure_server_record(db, server_id, now):
    record = db.get(server_id)
    if isinstance(record, dict):
        record.setdefault("first_seen", now)
        record.setdefault("last_notified_cycle", -1)
        return record

    if isinstance(record, (int, float)):
        return {
            "first_seen": float(record),
            "last_notified_cycle": -1,
        }

    return {
        "first_seen": now,
        "last_notified_cycle": -1,
    }


def main():
    if not WEBHOOK_URL:
        print("Перед запуском задай переменную DISCORD_WEBHOOK_URL.")
        print("PowerShell:")
        print('$env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."')
        return

    db = load_db()
    print("Бот запущен...")

    while True:
        try:
            now = time.time()
            servers = get_servers()
            active_ids = set()
            candidates = []

            for server in servers:
                server_id = server.get("id")
                players = int(server.get("playing") or 0)

                if not server_id or players < 1:
                    continue

                active_ids.add(server_id)

                if server_id not in db:
                    print(f"[+] Новый сервер: {server_id}")

                record = ensure_server_record(db, server_id, now)
                db[server_id] = record

                age = now - float(record["first_seen"])
                cycle = int(age // RIFT_INTERVAL)
                seconds_to_rift = int(RIFT_INTERVAL - (age % RIFT_INTERVAL))

                if 0 < seconds_to_rift <= RIFT_WINDOW:
                    candidates.append((server_id, seconds_to_rift, int(age), cycle, players))

            for server_id in list(db):
                if server_id not in active_ids:
                    del db[server_id]

            candidates.sort(key=lambda item: item[1])

            sent = 0
            for server_id, seconds_to_rift, age, cycle, players in candidates[:5]:
                record = ensure_server_record(db, server_id, now)
                if record.get("last_notified_cycle") == cycle:
                    continue

                link = f"https://www.roblox.com/games/{PLACE_ID}?gameInstanceId={server_id}"
                message = (
                    "Rift incoming!\n\n"
                    f"Uptime: {format_time(age)}\n"
                    f"Rift через: {format_time(seconds_to_rift)}\n"
                    f"Игроков: {players}\n"
                    f"{link}"
                )

                send_webhook(message)
                record["last_notified_cycle"] = cycle
                db[server_id] = record
                sent += 1
                print(f"[>] Отправлено: {server_id}")

            save_db(db)
            print(f"Проверено серверов: {len(active_ids)}. Отправлено: {sent}.")
            time.sleep(CHECK_DELAY)

        except requests.HTTPError as error:
            print(f"HTTP ошибка: {error}")
            time.sleep(30)
        except requests.RequestException as error:
            print(f"Ошибка сети: {error}")
            time.sleep(30)
        except KeyboardInterrupt:
            print("Бот остановлен.")
            break
        except Exception as error:
            print(f"Неожиданная ошибка: {error}")
            time.sleep(10)


if __name__ == "__main__":
    configure_console()
    main()