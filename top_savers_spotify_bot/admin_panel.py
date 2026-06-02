from __future__ import annotations

import csv
import html
import json
import platform
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Config
from .storage import Storage


ADMIN_COMMANDS: dict[int, str] = {
    1: "/bot_status",
    2: "/health_check",
    3: "/users_count",
    4: "/users_top",
    5: "/top_downloads",
    6: "/platform_stats",
    7: "/cache_stats",
    8: "/errors",
    9: "/recent_downloads",
    10: "/failed_downloads",
    11: "/db_tables",
    12: "/db_export",
    13: "/table_export users",
    14: "/ad_overview",
    15: "/req_overview",
    17: "/admin_list",
    18: "/add_admin 123456789",
    19: "/del_admin 123456789",
    20: "/friends_list",
    21: "/platform_health",
    23: "/daily_report",
    24: "/maintenance_on",
    25: "/maintenance_off",
    26: "/maintenance_status",
    27: "/ban 123456789",
    28: "/unban 123456789",
    29: "/banned",
    30: "/reports",
    31: "/ad8_list",
    32: "/broadcast",
    33: "/welcome_status",
    34: "/cleanup_temp 24",
    35: "/users",
    36: "/user 123456789 10",
}


def admin_help_messages() -> list[str]:
    return [
        "\n".join(
            [
                "<b>Админ-панель</b>",
                "Быстрый запуск: <code>/admin_run 1</code>",
                "Команды с аргументами запускай полной командой.",
                "",
                "<b>Диагностика и статистика</b>",
                "1. <code>/bot_status</code> - состояние бота, webhook и БД",
                "2. <code>/health_check</code> - быстрая диагностика окружения",
                "3. <code>/users_count</code> - количество пользователей и активность",
                "4. <code>/users_top</code> - топ пользователей по скачиваниям",
                "5. <code>/top_downloads</code> - топ ссылок, песен и альбомов",
                "6. <code>/platform_stats</code> - статистика по платформам",
                "7. <code>/cache_stats</code> - статистика кеша Telegram file_id",
                "8. <code>/errors</code> - последние ошибки бота",
                "9. <code>/recent_downloads</code> - последние скачивания",
                "10. <code>/failed_downloads</code> - последние неудачные скачивания",
                "11. <code>/db_tables</code> - таблицы БД и количество строк",
                "12. <code>/db_export</code> - выгрузить SQLite базу",
                "13. <code>/table_export users</code> - выгрузить таблицу CSV",
                "14. <code>/ad_overview</code> - сводка рекламы после выдачи",
                "15. <code>/req_overview</code> - сводка обязательных подписок",
                "21. <code>/platform_health</code> - здоровье платформ за 24 часа",
                "23. <code>/daily_report</code> - ручной ежедневный отчет админу",
            ]
        ),
        "\n".join(
            [
                "<b>Управление</b>",
                "17. <code>/admin_list</code> - список админов",
                "18. <code>/add_admin 123456789</code> - добавить админа",
                "19. <code>/del_admin 123456789</code> - убрать админа из БД",
                "20. <code>/friend_list</code> - друзья без рекламы",
                "24. <code>/maintenance_on [text]</code> - включить обслуживание",
                "25. <code>/maintenance_off</code> - выключить обслуживание",
                "26. <code>/maintenance_status</code> - статус обслуживания",
                "27. <code>/ban 123456789</code> - заблокировать пользователя",
                "28. <code>/unban 123456789</code> - разблокировать пользователя",
                "29. <code>/banned</code> - список заблокированных",
                "30. <code>/reports</code> - жалобы пользователей",
                "31. <code>/ad8_list</code> - реклама каждые 8 часов",
                "32. <code>/broadcast</code> - рассылка всем пользователям",
                "33. <code>/welcome_status</code> - приветствие /start и кеш фото",
                "34. <code>/cleanup_temp 24</code> - очистка старых временных файлов",
                "35. <code>/users</code> - последние 30 пользователей",
                "36. <code>/user 123456789</code> - карточка пользователя и последние скачивания",
                "",
                "<b>Реклама после скачивания</b>",
                "<code>/ad_add</code> ответом на сообщение - добавить рекламу",
                "<code>/ad_add Текст | https://url | green</code> - добавить кнопку",
                "<code>/ad_list</code>, <code>/ad_on 1</code>, <code>/ad_off 1</code>, <code>/ad_del 1</code>",
                "<code>/ad_stats</code>, <code>/ad_stats 1</code>, <code>/ad_stats_txt</code>",
                "",
                "<b>Реклама каждые 8 часов</b>",
                "<code>/ad8_add</code> ответом на сообщение - добавить рекламу",
                "<code>/ad8_list</code>, <code>/ad8_on 1</code>, <code>/ad8_off 1</code>, <code>/ad8_del 1</code>",
                "<code>/ad8_stats</code>, <code>/ad8_stats 1</code>, <code>/ad8_stats_txt</code>",
                "",
                "<b>Рассылка и друзья</b>",
                "<code>/broadcast</code> или <code>/bc</code> ответом на сообщение - разослать копию",
                "<code>/broadcast Кнопка | https://url | green</code> - рассылка с кнопкой",
                "<code>/friend_add 123456789</code>, <code>/friend_del 123456789</code>, <code>/friend_list</code>",
            ]
        ),
    ]


def create_admin_router(config: Config, storage: Storage) -> Router:
    router = Router(name="admin_panel")

    async def admin_only(message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        if user_id in config.admin_ids or await storage.is_admin_user(user_id):
            return True
        await message.answer("Команда доступна только админам.")
        return False

    async def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await storage.db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def query_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await query_all(sql, params)
        return rows[0] if rows else None

    async def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = await query_one(sql, params)
        if not row:
            return 0
        return int(next(iter(row.values())) or 0)

    async def download_history(limit: int, user_id: int | None = None) -> list[dict[str, Any]]:
        downloads_filter = ""
        failures_filter = ""
        params: tuple[Any, ...]

        if user_id is None:
            params = (limit,)
        else:
            downloads_filter = "WHERE d.user_id=?"
            failures_filter = "WHERE f.user_id=?"
            params = (user_id, user_id, limit)

        return await query_all(
            f"""
            SELECT *
            FROM (
                SELECT
                    d.id AS id,
                    d.created_at AS created_at,
                    d.user_id AS user_id,
                    u.username AS username,
                    CASE
                        WHEN a.album_id IS NOT NULL THEN 'spotify/album'
                        ELSE 'spotify/track'
                    END AS media_type,
                    COALESCE(t.spotify_url, a.spotify_url, ta.source_url, '') AS media_url,
                    'sent' AS status,
                    CASE WHEN LOWER(COALESCE(d.source, '')) LIKE '%cache%' THEN 'hit' ELSE 'miss' END AS cache_status,
                    1 AS items_sent,
                    COALESCE(t.title, a.name, d.object_id) AS title,
                    '' AS error_text
                FROM downloads d
                LEFT JOIN users u ON u.user_id=d.user_id
                LEFT JOIN cached_tracks t ON t.track_id=d.object_id
                LEFT JOIN cached_albums a ON a.album_id=d.object_id
                LEFT JOIN track_aliases ta ON ta.source='track_id' AND ta.source_id=d.object_id
                {downloads_filter}

                UNION ALL

                SELECT
                    f.id AS id,
                    f.created_at AS created_at,
                    f.user_id AS user_id,
                    u.username AS username,
                    COALESCE(f.platform, 'spotify') || '/' ||
                        CASE
                            WHEN a.album_id IS NOT NULL THEN 'album'
                            WHEN t.track_id IS NOT NULL THEN 'track'
                            ELSE 'audio'
                        END AS media_type,
                    COALESCE(t.spotify_url, a.spotify_url, ta.source_url, '') AS media_url,
                    'failed' AS status,
                    'miss' AS cache_status,
                    0 AS items_sent,
                    COALESCE(t.title, a.name, f.object_id, '-') AS title,
                    f.reason AS error_text
                FROM failed_downloads f
                LEFT JOIN users u ON u.user_id=f.user_id
                LEFT JOIN cached_tracks t ON t.track_id=f.object_id
                LEFT JOIN cached_albums a ON a.album_id=f.object_id
                LEFT JOIN track_aliases ta ON ta.source='track_id' AND ta.source_id=f.object_id
                {failures_filter}
            )
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            params,
        )

    async def resolve_user(target: str) -> dict[str, Any] | None:
        if target.startswith("@"):
            username = target[1:].strip().lower()
            if not username:
                return None
            return await query_one("SELECT * FROM users WHERE lower(username)=? LIMIT 1", (username,))

        try:
            user_id = int(target)
        except ValueError:
            return None

        return await query_one("SELECT * FROM users WHERE user_id=? LIMIT 1", (user_id,))

    @router.message(Command("admin_run"))
    async def admin_run(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        try:
            number = int((command.args or "").strip())
        except ValueError:
            await message.answer(command_menu())
            return
        await message.answer(ADMIN_COMMANDS.get(number, "Команда с таким номером не найдена."))

    @router.message(Command("bot_status"))
    async def bot_status(message: Message, bot: Bot) -> None:
        if not await admin_only(message):
            return
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        db_ok = storage.path.exists()
        maintenance = await storage.is_maintenance_on()
        await message.answer(
            "\n".join(
                [
                    f"Bot: @{me.username} ({me.id})",
                    f"Polling/webhook: {'webhook set' if webhook.url else 'polling'}",
                    f"Webhook pending: {webhook.pending_update_count}",
                    f"DB: {'ok' if db_ok else 'missing'} {html.escape(str(storage.path))}",
                    f"Maintenance: {'ON' if maintenance else 'OFF'}",
                ]
            )
        )

    @router.message(Command("health_check"))
    async def health_check(message: Message) -> None:
        if not await admin_only(message):
            return
        checks = [
            f"Python: {platform.python_version()}",
            f"Platform: {html.escape(platform.platform())}",
            f"Executable: {html.escape(sys.executable)}",
            f"DB exists: {storage.path.exists()}",
            f"Audio dir: {config.audio_library_dir.exists()}",
            f"Cover cache: {config.cover_cache_dir.exists()}",
            f"Spotify client id: {'set' if config.spotify_client_id else 'missing'}",
            f"Authorized audio API: {len(config.authorized_audio_api_urls)} endpoint(s)",
        ]
        await message.answer("\n".join(checks))

    @router.message(Command("users_count"))
    async def users_count(message: Message) -> None:
        if not await admin_only(message):
            return
        total = await scalar("SELECT COUNT(*) FROM users")
        active_24h = await scalar("SELECT COUNT(*) FROM users WHERE datetime(last_seen) >= datetime('now', '-1 day')")
        active_7d = await scalar("SELECT COUNT(*) FROM users WHERE datetime(last_seen) >= datetime('now', '-7 days')")
        premium = await scalar("SELECT COUNT(*) FROM users WHERE is_premium=1 OR subscription_status IN ('active','premium')")
        friends = await scalar("SELECT COUNT(*) FROM friends")
        banned = await scalar("SELECT COUNT(*) FROM banned_users")
        await message.answer(
            f"Всего: {total}\nАктивны 24ч: {active_24h}\nАктивны 7д: {active_7d}\nPremium: {premium}\nДрузья: {friends}\nБан: {banned}"
        )

    @router.message(Command("users_top"))
    async def users_top(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all(
            """
            SELECT user_id, username, first_name, downloads_count
            FROM users
            ORDER BY downloads_count DESC, last_seen DESC
            LIMIT 20
            """
        )
        await message.answer(format_rows(rows, ["user_id", "username", "first_name", "downloads_count"], "Топ пользователей пуст."))

    @router.message(Command("top_downloads"))
    async def top_downloads(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all(
            """
            SELECT d.object_id, COALESCE(t.title, a.name, d.object_id) AS title, COUNT(*) AS count
            FROM downloads d
            LEFT JOIN cached_tracks t ON t.track_id=d.object_id
            LEFT JOIN cached_albums a ON a.album_id=d.object_id
            GROUP BY d.object_id
            ORDER BY count DESC
            LIMIT 20
            """
        )
        await message.answer(format_rows(rows, ["count", "title", "object_id"], "Скачиваний пока нет."))

    @router.message(Command("platform_stats"))
    async def platform_stats(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT source AS platform, COUNT(*) AS count FROM downloads GROUP BY source ORDER BY count DESC")
        await message.answer(format_rows(rows, ["platform", "count"], "Статистики платформ пока нет."))

    @router.message(Command("cache_stats"))
    async def cache_stats(message: Message) -> None:
        if not await admin_only(message):
            return
        file_cache = await scalar("SELECT COUNT(*) FROM file_cache")
        tracks = await scalar("SELECT COUNT(*) FROM cached_tracks")
        tracks_with_file = await scalar("SELECT COUNT(*) FROM cached_tracks WHERE file_id IS NOT NULL")
        track_audio = await scalar("SELECT COUNT(*) FROM track_audio_cache WHERE file_id IS NOT NULL")
        aliases = await scalar("SELECT COUNT(*) FROM track_aliases")
        albums = await scalar("SELECT COUNT(*) FROM cached_albums")
        await message.answer(
            f"Telegram file_id: {file_cache}\nCached tracks: {tracks}\nTracks with audio file_id: {tracks_with_file}\nTrack audio cache: {track_audio}\nTrack aliases: {aliases}\nCached albums: {albums}"
        )

    @router.message(Command("errors"))
    async def errors(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT id, scope, message, created_at FROM bot_errors ORDER BY id DESC LIMIT 20")
        await message.answer(format_rows(rows, ["id", "created_at", "scope", "message"], "Ошибок нет."))

    @router.message(Command("recent_downloads"))
    async def recent_downloads(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        limit = parse_limit(command.args, 10, 200)
        rows = await query_all(
            """
            SELECT *
            FROM (
                SELECT
                    d.id AS id,
                    d.created_at AS created_at,
                    d.user_id AS user_id,
                    u.username AS username,
                    CASE
                        WHEN a.album_id IS NOT NULL THEN 'spotify/album'
                        ELSE 'spotify/track'
                    END AS media_type,
                    COALESCE(t.spotify_url, a.spotify_url, ta.source_url, '') AS media_url,
                    'sent' AS status,
                    CASE WHEN LOWER(COALESCE(d.source, '')) LIKE '%cache%' THEN 'hit' ELSE 'miss' END AS cache_status,
                    1 AS items_sent,
                    COALESCE(t.title, a.name, d.object_id) AS title,
                    '' AS error_text
                FROM downloads d
                LEFT JOIN users u ON u.user_id=d.user_id
                LEFT JOIN cached_tracks t ON t.track_id=d.object_id
                LEFT JOIN cached_albums a ON a.album_id=d.object_id
                LEFT JOIN track_aliases ta ON ta.source='track_id' AND ta.source_id=d.object_id

                UNION ALL

                SELECT
                    f.id AS id,
                    f.created_at AS created_at,
                    f.user_id AS user_id,
                    u.username AS username,
                    COALESCE(f.platform, 'spotify') || '/' ||
                        CASE
                            WHEN a.album_id IS NOT NULL THEN 'album'
                            WHEN t.track_id IS NOT NULL THEN 'track'
                            ELSE 'audio'
                        END AS media_type,
                    COALESCE(t.spotify_url, a.spotify_url, ta.source_url, '') AS media_url,
                    'failed' AS status,
                    'miss' AS cache_status,
                    0 AS items_sent,
                    COALESCE(t.title, a.name, f.object_id, '-') AS title,
                    f.reason AS error_text
                FROM failed_downloads f
                LEFT JOIN users u ON u.user_id=f.user_id
                LEFT JOIN cached_tracks t ON t.track_id=f.object_id
                LEFT JOIN cached_albums a ON a.album_id=f.object_id
                LEFT JOIN track_aliases ta ON ta.source='track_id' AND ta.source_id=f.object_id
            )
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        await answer_chunks(message, format_recent_downloads(rows, "No downloads."))
        return
        await message.answer(format_recent_downloads(rows, "Скачиваний пока нет."))
        return

    @router.message(Command("failed_downloads"))
    async def failed_downloads(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT id, user_id, object_id, reason, created_at FROM failed_downloads ORDER BY id DESC LIMIT 30")
        await message.answer(format_rows(rows, ["id", "created_at", "user_id", "object_id", "reason"], "Неудачных скачиваний нет."))

    @router.message(Command("db_tables"))
    async def db_tables(message: Message) -> None:
        if not await admin_only(message):
            return
        tables = await query_all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        lines = []
        for table in tables:
            name = table["name"]
            count = await scalar(f"SELECT COUNT(*) FROM {quote_ident(name)}")
            lines.append(f"{name}: {count}")
        await message.answer("\n".join(lines) or "Таблиц нет.")

    @router.message(Command("db_export"))
    async def db_export(message: Message) -> None:
        if not await admin_only(message):
            return
        await message.answer_document(FSInputFile(storage.path), caption="SQLite export")

    @router.message(Command("table_export"))
    async def table_export(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        table = (command.args or "").strip()
        if not table:
            await message.answer("Использование: /table_export users")
            return
        if not await table_exists(table):
            await message.answer("Такой таблицы нет.")
            return
        rows = await query_all(f"SELECT * FROM {quote_ident(table)}")
        csv_text = rows_to_csv(rows)
        await message.answer_document(
            BufferedInputFile(csv_text.encode("utf-8-sig"), filename=f"{table}.csv"),
            caption=f"CSV: {table}",
        )

    @router.message(Command("ad_overview"))
    async def ad_overview(message: Message) -> None:
        if not await admin_only(message):
            return
        total = await scalar("SELECT COUNT(*) FROM ads WHERE is_deleted=0")
        active = await scalar("SELECT COUNT(*) FROM ads WHERE is_deleted=0 AND is_active=1")
        after = await scalar("SELECT COUNT(*) FROM ads WHERE placement='after_message' AND is_deleted=0")
        ad8 = await scalar("SELECT COUNT(*) FROM ads WHERE placement='every_8h' AND is_deleted=0")
        sent_24h = await scalar("SELECT COUNT(*) FROM ad_delivery_log WHERE datetime(sent_at) >= datetime('now', '-1 day')")
        await message.answer(f"Всего: {total}\nACTIVE: {active}\nПосле выдачи: {after}\nКаждые 8ч: {ad8}\nПоказов 24ч: {sent_24h}")

    @router.message(Command("req_overview"))
    async def req_overview(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT id, kind, title, value, is_active FROM required_links ORDER BY id")
        await message.answer(format_rows(rows, ["id", "kind", "is_active", "title", "value"], "Обязательных ссылок нет."))

    @router.message(Command("admin_list"))
    async def admin_list(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT user_id, source, created_at FROM admin_users ORDER BY created_at DESC")
        await message.answer(format_rows(rows, ["user_id", "source", "created_at"], "Админов в БД нет."))

    @router.message(Command("add_admin", "admin_add"))
    async def admin_add(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        user_id = parse_user_id(command.args)
        if not user_id:
            await message.answer("Использование: /add_admin 123456789")
            return
        await storage.set_admin(user_id, True)
        await message.answer(f"Админ добавлен: {user_id}")

    @router.message(Command("del_admin", "admin_del"))
    async def admin_del(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        user_id = parse_user_id(command.args)
        if not user_id:
            await message.answer("Использование: /del_admin 123456789")
            return
        await storage.set_admin(user_id, False)
        await message.answer(f"Админ удален из БД: {user_id}")

    @router.message(Command("friends_list", "friend_list"))
    async def friend_list(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT user_id, note, created_at FROM friends ORDER BY created_at DESC")
        await message.answer(format_rows(rows, ["user_id", "note", "created_at"], "Друзей без рекламы нет."))

    @router.message(Command("friends_add", "friend_add", "add_friend"))
    async def friend_add(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        user_id = parse_user_id(parts[0] if parts else None)
        if not user_id:
            await message.answer("Использование: /friend_add 123456789 [заметка]")
            return
        await storage.set_friend(user_id, True, parts[1] if len(parts) > 1 else None)
        await message.answer(f"Друг без рекламы добавлен: {user_id}")

    @router.message(Command("friends_del", "friend_del", "del_friend"))
    async def friend_del(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        user_id = parse_user_id(command.args)
        if not user_id:
            await message.answer("Использование: /friend_del 123456789")
            return
        await storage.set_friend(user_id, False)
        await message.answer(f"Друг удален: {user_id}")

    @router.message(Command("platform_health"))
    async def platform_health(message: Message) -> None:
        if not await admin_only(message):
            return
        downloads = await scalar("SELECT COUNT(*) FROM downloads WHERE datetime(created_at) >= datetime('now', '-1 day')")
        failed = await scalar("SELECT COUNT(*) FROM failed_downloads WHERE datetime(created_at) >= datetime('now', '-1 day')")
        errors_count = await scalar("SELECT COUNT(*) FROM bot_errors WHERE datetime(created_at) >= datetime('now', '-1 day')")
        await message.answer(f"Spotify 24h\nDownloads: {downloads}\nFailed: {failed}\nErrors: {errors_count}")

    @router.message(Command("daily_report"))
    async def daily_report(message: Message) -> None:
        if not await admin_only(message):
            return
        total_users = await scalar("SELECT COUNT(*) FROM users")
        new_users = await scalar("SELECT COUNT(*) FROM users WHERE datetime(created_at) >= datetime('now', '-1 day')")
        downloads = await scalar("SELECT COUNT(*) FROM downloads WHERE datetime(created_at) >= datetime('now', '-1 day')")
        failed = await scalar("SELECT COUNT(*) FROM failed_downloads WHERE datetime(created_at) >= datetime('now', '-1 day')")
        ads_sent = await scalar("SELECT COUNT(*) FROM ad_delivery_log WHERE datetime(sent_at) >= datetime('now', '-1 day')")
        await message.answer(f"Daily report\nUsers: {total_users} (+{new_users})\nDownloads 24h: {downloads}\nFailed 24h: {failed}\nAds sent 24h: {ads_sent}")

    @router.message(Command("maintenance_on"))
    async def maintenance_on(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        text = (command.args or "").strip() or "Бот на техническом обслуживании. Попробуйте позже."
        await storage.set_maintenance(True, text)
        await message.answer("Maintenance ON.")

    @router.message(Command("maintenance_off"))
    async def maintenance_off(message: Message) -> None:
        if not await admin_only(message):
            return
        await storage.set_maintenance(False)
        await message.answer("Maintenance OFF.")

    @router.message(Command("maintenance_status"))
    async def maintenance_status(message: Message) -> None:
        if not await admin_only(message):
            return
        enabled = await storage.is_maintenance_on()
        text = await storage.get_setting("maintenance_text")
        await message.answer(f"Maintenance: {'ON' if enabled else 'OFF'}\n{text or ''}")

    @router.message(Command("ban"))
    async def ban(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        user_id = parse_user_id(parts[0] if parts else None)
        if not user_id:
            await message.answer("Использование: /ban 123456789 [reason]")
            return
        await storage.ban_user(user_id, parts[1] if len(parts) > 1 else None)
        await message.answer(f"Пользователь заблокирован: {user_id}")

    @router.message(Command("unban"))
    async def unban(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        user_id = parse_user_id(command.args)
        if not user_id:
            await message.answer("Использование: /unban 123456789")
            return
        await storage.unban_user(user_id)
        await message.answer(f"Пользователь разблокирован: {user_id}")

    @router.message(Command("banned"))
    async def banned(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT user_id, reason, created_at FROM banned_users ORDER BY created_at DESC LIMIT 50")
        await message.answer(format_rows(rows, ["user_id", "reason", "created_at"], "Заблокированных нет."))

    @router.message(Command("reports"))
    async def reports(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT id, user_id, text, status, created_at FROM reports ORDER BY id DESC LIMIT 30")
        await message.answer(format_rows(rows, ["id", "created_at", "user_id", "status", "text"], "Жалоб нет."))

    @router.message(Command("report"))
    async def report(message: Message, command: CommandObject) -> None:
        user_id = message.from_user.id if message.from_user else 0
        text = (command.args or "").strip()
        if not text:
            await message.answer("Использование: /report текст жалобы")
            return
        report_id = await storage.add_report(user_id, text)
        await message.answer(f"Жалоба принята, id: {report_id}")

    @router.message(Command("ad_add"))
    async def ad_add(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        await add_reply_ad(message, command, "after_message")

    @router.message(F.text.startswith("add "))
    async def legacy_add(message: Message) -> None:
        if not await admin_only(message):
            return
        class LegacyCommand:
            args = (message.text or "")[4:].strip()

        await add_reply_ad(message, LegacyCommand(), "after_message")

    @router.message(Command("ad8_add"))
    async def ad8_add(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        await add_reply_ad(message, command, "every_8h")

    @router.message(Command("ad_list"))
    async def ad_list(message: Message) -> None:
        if not await admin_only(message):
            return
        await send_ad_list(message, "after_message")

    @router.message(Command("ad8_list"))
    async def ad8_list(message: Message) -> None:
        if not await admin_only(message):
            return
        await send_ad_list(message, "every_8h")

    @router.message(Command("ad_on", "ad8_on"))
    async def ad_on(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        ad_id = parse_user_id(command.args)
        if not ad_id:
            await message.answer("Использование: /ad_on 1")
            return
        await storage.set_ad_active(ad_id, True)
        await message.answer(f"Ad #{ad_id}: ACTIVE")

    @router.message(Command("ad_off", "ad8_off"))
    async def ad_off(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        ad_id = parse_user_id(command.args)
        if not ad_id:
            await message.answer("Использование: /ad_off 1")
            return
        await storage.set_ad_active(ad_id, False)
        await message.answer(f"Ad #{ad_id}: PAUSED")

    @router.message(Command("ad_del", "ad8_del"))
    async def ad_del(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        ad_id = parse_user_id(command.args)
        if not ad_id:
            await message.answer("Использование: /ad_del 1")
            return
        await storage.delete_ad(ad_id)
        await message.answer(f"Ad #{ad_id}: deleted from active list")

    @router.message(Command("ad_stats", "ad8_stats"))
    async def ad_stats(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        ad_id = parse_user_id(command.args)
        if ad_id:
            ad = await storage.get_ad(ad_id)
            if not ad:
                await message.answer("Реклама не найдена.")
                return
            views = await scalar("SELECT COUNT(*) FROM ad_delivery_log WHERE ad_id=?", (ad_id,))
            await message.answer(format_ad(ad) + f"\nПоказов: {views}")
            return
        rows = await query_all(
            """
            SELECT a.id, a.placement, a.status, a.text, COUNT(l.id) AS views
            FROM ads a
            LEFT JOIN ad_delivery_log l ON l.ad_id=a.id
            GROUP BY a.id
            ORDER BY a.id DESC
            LIMIT 30
            """
        )
        await message.answer(format_rows(rows, ["id", "placement", "status", "views", "text"], "История рекламы пуста."))

    @router.message(Command("ad_stats_txt", "ad8_stats_txt"))
    async def ad_stats_txt(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all("SELECT * FROM ads ORDER BY id DESC")
        text = "\n\n".join(format_ad(row) for row in rows) or "No ads"
        await message.answer_document(BufferedInputFile(text.encode("utf-8"), filename="ads_stats.txt"))

    @router.message(Command("broadcast", "bc"))
    async def broadcast(message: Message, command: CommandObject, bot: Bot) -> None:
        if not await admin_only(message):
            return
        buttons = parse_buttons(command.args or "")
        users = await storage.all_user_ids()
        sent = 0
        failed = 0
        keyboard = build_keyboard(buttons)
        if message.reply_to_message:
            for user_id in users:
                if await storage.is_banned(user_id):
                    continue
                try:
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=message.reply_to_message.chat.id,
                        message_id=message.reply_to_message.message_id,
                        reply_markup=keyboard,
                    )
                    sent += 1
                except (TelegramBadRequest, TelegramForbiddenError):
                    failed += 1
        else:
            text = (command.args or "").strip()
            if not text:
                await message.answer("Ответь на сообщение командой /broadcast или напиши /broadcast текст")
                return
            for user_id in users:
                if await storage.is_banned(user_id):
                    continue
                try:
                    await bot.send_message(user_id, text, reply_markup=keyboard)
                    sent += 1
                except (TelegramBadRequest, TelegramForbiddenError):
                    failed += 1
        await message.answer(f"Broadcast done. Sent: {sent}, failed: {failed}")

    @router.message(Command("welcome_set"))
    async def welcome_set(message: Message) -> None:
        if not await admin_only(message):
            return
        reply = message.reply_to_message
        if not reply or not reply.photo:
            await message.answer("Ответь на фото командой /welcome_set.")
            return
        file_id = reply.photo[-1].file_id
        await storage.set_setting("welcome_photo_file_id", file_id)
        await message.answer("Welcome photo cached.")

    @router.message(Command("welcome_clear"))
    async def welcome_clear(message: Message) -> None:
        if not await admin_only(message):
            return
        await storage.set_setting("welcome_photo_file_id", "")
        await message.answer("Welcome photo cache cleared.")

    @router.message(Command("welcome_status"))
    async def welcome_status(message: Message) -> None:
        if not await admin_only(message):
            return
        file_id = await storage.get_setting("welcome_photo_file_id")
        await message.answer(f"Welcome photo: {'set' if file_id else 'empty'}\n/start text: i18n TextKey.START")

    @router.message(Command("cleanup_temp"))
    async def cleanup_temp(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        try:
            hours = int((command.args or "24").strip())
        except ValueError:
            hours = 24
        removed = cleanup_old_files(config.cover_cache_dir, hours)
        removed += cleanup_old_files(config.audio_library_dir / ".authorized-cache", hours)
        await message.answer(f"Removed files: {removed}")

    @router.message(Command("users"))
    async def users(message: Message) -> None:
        if not await admin_only(message):
            return
        rows = await query_all(
            """
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                u.created_at,
                u.last_seen,
                COALESCE(s.successful, 0) AS successful,
                COALESCE(c.cache_hits, 0) AS cache_hits,
                COALESCE(s.successful, 0) + COALESCE(f.failed, 0) AS requests
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS successful
                FROM downloads
                GROUP BY user_id
            ) s ON s.user_id = u.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS cache_hits
                FROM downloads
                WHERE LOWER(COALESCE(source, '')) LIKE '%cache%'
                GROUP BY user_id
            ) c ON c.user_id = u.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS failed
                FROM failed_downloads
                WHERE user_id IS NOT NULL
                GROUP BY user_id
            ) f ON f.user_id = u.user_id
            ORDER BY u.created_at DESC
            LIMIT 30
            """
        )
        await message.answer(format_users(rows, "Пользователей нет."))
        return

    @router.message(Command("user"))
    async def user(message: Message, command: CommandObject) -> None:
        if not await admin_only(message):
            return
        target, limit = parse_user_lookup(command.args)
        if not target:
            await message.answer("Usage: /user 123456789 10 or /user @username 10")
            return
        row = await resolve_user(target)
        if not row:
            await message.answer("User not found.")
            return
        user_id = int(row["user_id"])
        downloads = await download_history(limit, user_id)
        lines = [
            f"User {html.escape(str(user_id))} {html.escape(username_label(row.get('username')))}",
            f"Recent downloads ({len(downloads)}/{limit})",
            format_user_downloads(downloads, "No downloads."),
        ]
        await answer_chunks(message, "\n".join(lines))
        return
        user_id = parse_user_id(command.args)
        if not user_id:
            await message.answer("Использование: /user 123456789")
            return
        row = await query_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        if not row:
            await message.answer("Пользователь не найден.")
            return
        downloads = await query_all(
            """
            SELECT d.created_at, COALESCE(t.title, d.object_id) AS title, d.source
            FROM downloads d
            LEFT JOIN cached_tracks t ON t.track_id=d.object_id
            WHERE d.user_id=?
            ORDER BY d.id DESC
            LIMIT 10
            """,
            (user_id,),
        )
        lines = [
            f"User: {user_id}",
            f"Username: @{row.get('username')}" if row.get("username") else "Username: -",
            f"First seen: {row.get('created_at')}",
            f"Last seen: {row.get('last_seen')}",
            f"Downloads: {row.get('downloads_count')}",
            "",
            "Recent downloads:",
        ]
        lines.extend(format_row(download, ["created_at", "source", "title"]) for download in downloads)
        await message.answer("\n".join(lines))

    async def add_reply_ad(message: Message, command: CommandObject, placement: str) -> None:
        reply = message.reply_to_message
        if not reply:
            await message.answer("Пришли рекламное сообщение и ответь на него этой командой.")
            return
        text = reply.text or reply.caption or ""
        media_type, media_file_id = extract_media(reply)
        buttons = parse_buttons(command.args or "")
        ad_id = await storage.add_ad(
            placement,
            text,
            name=placement,
            media_type=media_type,
            media_file_id=media_file_id,
            source_chat_id=reply.chat.id,
            source_message_id=reply.message_id,
            buttons=buttons,
        )
        await message.answer(f"Реклама добавлена: #{ad_id} ({placement})")

    async def send_ad_list(message: Message, placement: str) -> None:
        ads = [ad for ad in await storage.list_ads() if ad["placement"] == placement]
        if not ads:
            await message.answer("Реклам нет.")
            return
        await message.answer("\n".join(format_ad(ad) for ad in ads[:30]))

    async def table_exists(table: str) -> bool:
        row = await query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return row is not None

    return router


def parse_buttons(raw: str) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    raw = raw.strip()
    if not raw:
        return buttons
    for chunk in raw.split("||"):
        parts = [part.strip() for part in chunk.split("|")]
        if len(parts) < 2:
            continue
        buttons.append(
            {
                "text": parts[0],
                "url": parts[1],
                "color": parts[2] if len(parts) > 2 else "default",
            }
        )
    return buttons


def build_keyboard(buttons: list[dict[str, str]] | str | None):
    if isinstance(buttons, str):
        try:
            buttons = json.loads(buttons)
        except json.JSONDecodeError:
            buttons = []
    if not buttons:
        return None
    builder = InlineKeyboardBuilder()
    for button in buttons:
        text = str(button.get("text", "")).strip()
        url = str(button.get("url", "")).strip()
        if text and url:
            builder.button(text=text, url=url)
    builder.adjust(1)
    return builder.as_markup()


def extract_media(message: Message) -> tuple[str | None, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.voice:
        return "voice", message.voice.file_id
    return None, None


async def send_stored_ad(bot: Bot, chat_id: int, ad: dict[str, Any]) -> None:
    keyboard = build_keyboard(ad.get("buttons_json"))
    source_chat_id = ad.get("source_chat_id")
    source_message_id = ad.get("source_message_id")
    if source_chat_id and source_message_id:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(source_chat_id),
            message_id=int(source_message_id),
            reply_markup=keyboard,
        )
        return
    await bot.send_message(chat_id, str(ad.get("text") or ""), reply_markup=keyboard)


def parse_user_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw.strip().split()[0])
    except (ValueError, IndexError):
        return None


def parse_limit(raw: str | None, default: int = 10, maximum: int = 200) -> int:
    if not raw:
        return default
    parts = raw.strip().split()
    if not parts:
        return default
    try:
        value = int(parts[0])
    except ValueError:
        return default
    return max(1, min(value, maximum))


def parse_user_lookup(raw: str | None, default_limit: int = 10, maximum: int = 200) -> tuple[str | None, int]:
    parts = (raw or "").strip().split()
    if not parts:
        return None, default_limit
    limit = parse_limit(parts[1] if len(parts) > 1 else None, default_limit, maximum)
    return parts[0], limit


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


async def answer_chunks(message: Message, text: str, limit: int = 3900) -> None:
    parts: list[str] = []
    current = ""

    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = line

    if current:
        parts.append(current)

    for part in parts or [text]:
        await message.answer(part)


def format_recent_downloads(rows: list[dict[str, Any]], empty: str) -> str:
    if not rows:
        return empty
    lines = ["Recent downloads"]
    for index, row in enumerate(rows, start=1):
        media_label = linked_media_label(row.get("media_type"), row.get("media_url"))
        username = html.escape(username_label(row.get("username")))
        text = compact_download_text(row.get("error_text") or row.get("title"))
        lines.append(
            (
                f"{index}. #{row.get('id')} {html.escape(date_minute(row.get('created_at')))} | "
                f"user {html.escape(str(row.get('user_id') or '-'))} {username} | "
                f"{media_label} | {html.escape(str(row.get('status') or '-'))} "
                f"{html.escape(str(row.get('cache_status') or '-'))} | "
                f"items {int(row.get('items_sent') or 0)} | {html.escape(text)}"
            )
        )
    return "\n".join(lines)


def format_user_downloads(rows: list[dict[str, Any]], empty: str) -> str:
    if not rows:
        return empty
    lines = []
    for index, row in enumerate(rows, start=1):
        media_label = linked_media_label(row.get("media_type"), row.get("media_url"))
        text = compact_download_text(row.get("error_text") or row.get("title"))
        lines.append(
            (
                f"{index}. {html.escape(date_minute(row.get('created_at')))} | "
                f"{media_label} | {html.escape(str(row.get('status') or '-'))} "
                f"{html.escape(str(row.get('cache_status') or '-'))} | "
                f"items {int(row.get('items_sent') or 0)} | {html.escape(text)}"
            )
        )
    return "\n".join(lines)


def format_users(rows: list[dict[str, Any]], empty: str) -> str:
    if not rows:
        return empty
    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        username = compact_user_field(row.get("username"))
        if username != "-" and not username.startswith("@"):
            username = f"@{username}"
        name = compact_user_field(row.get("first_name"))
        lines.append(
            " | ".join(
                [
                    f"{index} {row.get('user_id')}",
                    username,
                    name,
                    date_only(row.get("created_at")),
                    date_only(row.get("last_seen")),
                    str(int(row.get("requests") or 0)),
                    str(int(row.get("successful") or 0)),
                    str(int(row.get("cache_hits") or 0)),
                ]
            )
        )
    return html.escape("\n".join(lines))[:3900]


def date_only(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if text else "-"


def date_minute(value: Any) -> str:
    text = str(value or "").strip()
    return text[:16] if text else "-"


def username_label(value: Any) -> str:
    username = compact_user_field(value)
    if username == "-":
        return "-"
    return username if username.startswith("@") else f"@{username}"


def linked_media_label(media_type: Any, url: Any) -> str:
    label = str(media_type or "unknown/unknown").strip() or "unknown/unknown"
    clean_url = str(url or "").strip()
    safe_label = html.escape(label)
    if not clean_url:
        return safe_label
    return f'<a href="{html.escape(clean_url, quote=True)}">{safe_label}</a>'


def compact_download_text(value: Any) -> str:
    text = str(value or "-").replace("\n", " ").strip()
    return " ".join(text.split())[:220] or "-"


def compact_user_field(value: Any) -> str:
    text = str(value or "").replace("|", " ").replace("\n", " ").strip()
    text = " ".join(text.split())
    return text or "-"


def format_rows(rows: list[dict[str, Any]], fields: list[str], empty: str) -> str:
    if not rows:
        return empty
    lines = [format_row(row, fields) for row in rows]
    text = "\n".join(lines)
    return text[:3900]


def format_row(row: dict[str, Any], fields: list[str]) -> str:
    return " | ".join(f"{field}: {html.escape(str(row.get(field, '-')))}" for field in fields)


def format_ad(ad: dict[str, Any]) -> str:
    buttons = ad.get("buttons_json") or "[]"
    try:
        buttons_count = len(json.loads(buttons))
    except json.JSONDecodeError:
        buttons_count = 0
    text = str(ad.get("text") or "").replace("\n", " ")[:80]
    status = ad.get("status") or ("ACTIVE" if ad.get("is_active") else "PAUSED")
    return f"#{ad.get('id')} {ad.get('placement')} {status} buttons:{buttons_count} media:{ad.get('media_type') or '-'} text:{html.escape(text)}"


def command_menu() -> str:
    return "\n".join(f"{number}. {command}" for number, command in ADMIN_COMMANDS.items())


def cleanup_old_files(directory: Path, hours: int) -> int:
    if not directory.exists():
        return 0
    import time

    cutoff = time.time() - hours * 3600
    removed = 0
    for path in directory.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
