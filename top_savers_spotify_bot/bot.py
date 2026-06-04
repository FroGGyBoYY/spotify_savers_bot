from __future__ import annotations

import asyncio
import html
import json
import time
import traceback
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .admin_panel import admin_help_messages, create_admin_router, send_stored_ad
from .audio_provider import AuthorizedAudioProvider, AuthorizedAudioProviderError
from .config import Config
from .cover_art import prepare_audio_thumbnail
from .emojis import custom_emoji, emoji_id
from .external_music import ExternalMusicResolver, ExternalMusicResolverError
from .i18n import SUPPORTED_LANGS, pick_lang, t
from .library import AudioLibrary
from .link_recognizer import LinkKind, recognize_link
from .parser import SpotifyLink
from .source_resolver import legal_source_links, youtube_music_search_url
from .spotify import (
    SpotifyAuthError,
    SpotifyClient,
    SpotifyCollection,
    SpotifyError,
    SpotifyNotFound,
    SpotifyPremiumRequired,
    SpotifyTrack,
)
from .storage import Storage
from .youtube_music import YouTubeMusicCandidate, YouTubeMusicMatcher, YouTubeMusicMatcherError
from .youtube_music_playlist import (
    YouTubeMusicPlaylist,
    YouTubeMusicPlaylistError,
    YouTubeMusicPlaylistResolver,
)


def build_dispatcher(
    config: Config,
    storage: Storage,
    spotify: SpotifyClient,
    library: AudioLibrary,
    audio_provider: AuthorizedAudioProvider | None = None,
) -> Dispatcher:
    router = Router()
    youtube_music_matcher = YouTubeMusicMatcher(config, spotify.session)
    youtube_music_playlist_resolver = YouTubeMusicPlaylistResolver(config)
    external_music_resolver = ExternalMusicResolver(
        spotify.session,
        config.vk_access_token,
        vk_browser_metadata_enabled=config.vk_browser_metadata_enabled,
        vk_browser_metadata_timeout_seconds=config.vk_browser_metadata_timeout_seconds,
    )
    admin_alert_cache: dict[str, float] = {}
    audio_download_locks: dict[str, asyncio.Lock] = {}
    recent_audio_sends: dict[str, float] = {}
    pending_problem_reports: set[int] = set()
    group_music_commands = {"music", "song", "track"}
    language_buttons = (
        ("ru", "Русский", "lang_ru"),
        ("en", "English", "lang_en"),
        ("es", "Español", "lang_es"),
        ("zh", "中文", "lang_zh"),
        ("ar", "العربية", "lang_ar"),
        ("th", "ไทย", "lang_th"),
    )

    def is_group_chat(message: Message) -> bool:
        chat_type = getattr(message.chat.type, "value", message.chat.type)
        return chat_type in {"group", "supergroup"}

    def group_music_command_payload(message: Message) -> str | None:
        text = (message.text or message.caption or "").strip()
        if not text.startswith("/"):
            return None

        command_token, _, payload = text.partition(" ")
        command = command_token[1:].split("@", 1)[0].lower()
        if command not in group_music_commands:
            return None

        payload = payload.strip()
        if not payload and message.reply_to_message:
            payload = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
        return payload

    async def remember_user(message: Message) -> str:
        user = message.from_user
        if user is None:
            return "ru"
        lang = pick_lang(user.language_code)
        await storage.upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=lang,
            is_admin=user.id in config.admin_ids,
        )
        return await storage.get_user_language(user.id) or lang

    async def lang_for_user(user_id: int | None, fallback: str | None = None) -> str:
        if user_id is None:
            return pick_lang(fallback)
        return await storage.get_user_language(user_id) or pick_lang(fallback)

    async def ensure_subscription(bot: Bot, user_id: int, lang: str, message: Message | None = None) -> bool:
        required_links = await storage.active_required_links()
        if not required_links:
            return True

        missing: list[str] = []
        for required in required_links:
            if required["kind"] != "telegram_channel":
                continue
            channel = str(required["value"])
            try:
                member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            except TelegramBadRequest:
                missing.append(display_required_link(required))
                continue
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(display_required_link(required))

        if not missing:
            return True

        text = t(lang, "subscription_required", channels=", ".join(missing))
        if message is not None:
            await message.answer(text)
        else:
            await bot.send_message(user_id, text)
        return False

    async def is_admin(user_id: int | None) -> bool:
        return user_id is not None and (user_id in config.admin_ids or await storage.is_admin_user(user_id))

    def audio_request_key(
        chat_id: int,
        user_id: int,
        track_id: str,
        source_id: str = "auto",
    ) -> str:
        return f"{chat_id}:{user_id}:{track_id}:{source_id or 'auto'}"

    def collection_request_key(chat_id: int, user_id: int, kind: str, spotify_id: str) -> str:
        return f"bundle:{chat_id}:{user_id}:{kind}:{spotify_id}"

    def youtube_music_playlist_request_key(chat_id: int, user_id: int, playlist_id: str) -> str:
        return f"ytmp:{chat_id}:{user_id}:{playlist_id}"

    def was_recent_audio_send(key: str, ttl_seconds: int = 20) -> bool:
        now = time.time()
        cleanup_ttl = max(120, ttl_seconds)
        for item_key, sent_at in list(recent_audio_sends.items()):
            if now - sent_at > cleanup_ttl:
                recent_audio_sends.pop(item_key, None)
        return now - recent_audio_sends.get(key, 0) < ttl_seconds

    def mark_recent_audio_send(key: str) -> None:
        recent_audio_sends[key] = time.time()

    async def telegram_retry_after_sleep(scope: str, error: TelegramRetryAfter) -> None:
        retry_after = max(1, int(getattr(error, "retry_after", 1) or 1))
        await storage.log_error("telegram_retry_after", f"{scope}: retry after {retry_after}s")
        await asyncio.sleep(retry_after + 1)

    async def send_message_with_retry(
        bot: Bot,
        chat_id: int,
        text: str,
        *,
        attempts: int = 5,
        **kwargs,
    ) -> Message:
        last_error: TelegramRetryAfter | None = None

        for attempt in range(max(1, attempts)):
            try:
                return await bot.send_message(chat_id, text, **kwargs)
            except TelegramRetryAfter as error:
                last_error = error
                if attempt >= attempts - 1:
                    raise
                await telegram_retry_after_sleep("send_message", error)

        raise last_error or RuntimeError("send_message failed")

    async def send_audio_with_retry(
        bot: Bot,
        chat_id: int,
        audio,
        *,
        attempts: int = 5,
        **kwargs,
    ) -> Message:
        last_error: TelegramRetryAfter | None = None

        for attempt in range(max(1, attempts)):
            try:
                return await bot.send_audio(chat_id, audio, **kwargs)
            except TelegramRetryAfter as error:
                last_error = error
                if attempt >= attempts - 1:
                    raise
                await telegram_retry_after_sleep("send_audio", error)

        raise last_error or RuntimeError("send_audio failed")

    def provider_cache_key(track: SpotifyTrack, source_candidate: dict[str, object]) -> str:
        source = str(source_candidate.get("source") or "youtube_music").strip()
        source_id = str(source_candidate.get("source_id") or source_candidate.get("id") or "").strip()
        if source_id:
            return f"{source}:{source_id}:{track.id}"
        return f"{source}:{track.id}"

    def bot_username() -> str:
        username = (config.expected_bot_username or config.bot_public_name).strip().lstrip("@")
        return username or "spotify_savers_bot"

    def language_label(code: str, lang: str | None = None) -> str:
        labels: dict[str, dict[str, str]] = {
            "ru": {"ru": "Русский", "en": "Russian", "es": "Ruso", "zh": "俄语", "ar": "الروسية", "th": "รัสเซีย"},
            "en": {"ru": "Английский", "en": "English", "es": "Inglés", "zh": "英语", "ar": "الإنجليزية", "th": "อังกฤษ"},
            "es": {"ru": "Испанский", "en": "Spanish", "es": "Español", "zh": "西班牙语", "ar": "الإسبانية", "th": "สเปน"},
            "zh": {"ru": "Китайский", "en": "Chinese", "es": "Chino", "zh": "中文", "ar": "الصينية", "th": "จีน"},
            "ar": {"ru": "Арабский", "en": "Arabic", "es": "Árabe", "zh": "阿拉伯语", "ar": "العربية", "th": "อาหรับ"},
            "th": {"ru": "Тайский", "en": "Thai", "es": "Tailandés", "zh": "泰语", "ar": "التايلاندية", "th": "ไทย"},
        }
        selected = pick_lang(lang)
        return labels.get(code, {}).get(selected) or code

    def add_to_group_url() -> str:
        return f"https://t.me/{bot_username()}?startgroup=true"

    def start_text(lang: str) -> str:
        return f"{t(lang, 'start')}\n\n{t(lang, 'language_prompt')}"

    def start_keyboard(lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for code, label, icon_name in language_buttons:
            builder.button(
                text=label,
                callback_data=f"lang:{code}",
                icon_custom_emoji_id=emoji_id(icon_name),
            )
        builder.button(
            text=t(lang, "add_to_group"),
            url=add_to_group_url(),
            icon_custom_emoji_id=emoji_id("globe"),
            style="success",
        )
        builder.adjust(2, 2, 2, 1)
        return builder.as_markup()

    def add_to_group_keyboard(lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t(lang, "add_to_group"),
            url=add_to_group_url(),
            icon_custom_emoji_id=emoji_id("globe"),
            style="success",
        )
        builder.adjust(1)
        return builder.as_markup()

    def cached_row_to_track(row: dict[str, object], fallback_url: str = "") -> SpotifyTrack:
        try:
            artists_payload = json.loads(str(row.get("artists_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            artists_payload = []
        artists = tuple(str(item) for item in artists_payload if str(item).strip())
        return SpotifyTrack(
            id=str(row.get("track_key") or ""),
            name=str(row.get("title") or ""),
            artists=artists,
            album="Cached audio",
            duration_ms=int(row.get("duration_ms") or 0),
            spotify_url=str(row.get("source_url") or fallback_url),
            image_url=str(row.get("image_url") or "") or None,
        )

    async def save_user_report(message: Message, bot: Bot, text: str, lang: str) -> None:
        user = message.from_user
        if user is None:
            return
        report_text = text.strip()[:3000]
        if not report_text:
            return
        report_id = await storage.add_report(user.id, report_text)
        await message.answer(t(lang, "help_sent"))
        admin_text = "\n".join(
            [
                f"<b>User report #{report_id}</b>",
                f"User: <code>{user.id}</code> @{html.escape(user.username or '-')}",
                f"Name: {html.escape(' '.join(part for part in [user.first_name, user.last_name] if part) or '-')}",
                "",
                html.escape(report_text[:2500]),
            ]
        )
        admin_ids = set(config.admin_ids)
        admin_ids.update(await storage.admin_user_ids())
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, admin_text)
            except (TelegramBadRequest, TelegramForbiddenError):
                continue

    def track_send_key(chat_id: int, user_id: int, track: SpotifyTrack) -> str:
        artists = " ".join(track.artists) or track.artist_names
        fingerprint = " ".join(f"{artists} {track.name}".casefold().split())
        return f"send:{chat_id}:{user_id}:{fingerprint}"

    @router.errors()
    async def errors_handler(event: ErrorEvent) -> None:
        await storage.log_error(
            "update",
            str(event.exception),
            "".join(traceback.format_exception(event.exception)),
        )

    @router.message(CommandStart())
    async def start(message: Message, bot: Bot) -> None:
        lang = await remember_user(message)
        user_id = message.from_user.id if message.from_user else 0
        if not await user_can_use_bot(message, user_id):
            return
        if not await ensure_subscription(bot, user_id, lang, message):
            return
        welcome_photo = await storage.get_setting("welcome_photo_file_id")
        if welcome_photo:
            await message.answer_photo(welcome_photo, caption=start_text(lang), reply_markup=start_keyboard(lang))
        else:
            await message.answer(start_text(lang), reply_markup=start_keyboard(lang))
        await send_interaction_ads(bot, message.chat.id, user_id)

    @router.message(Command("add_to_group"))
    async def add_to_group_command(message: Message, bot: Bot) -> None:
        lang = await remember_user(message)
        user_id = message.from_user.id if message.from_user else 0
        if not await user_can_use_bot(message, user_id):
            return
        await message.answer(t(lang, "add_to_group_info"), reply_markup=add_to_group_keyboard(lang))
        await send_interaction_ads(bot, message.chat.id, user_id)

    @router.message(Command("help"))
    async def help_command(message: Message, command: CommandObject, bot: Bot) -> None:
        lang = await remember_user(message)
        user_id = message.from_user.id if message.from_user else 0
        if not await user_can_use_bot(message, user_id):
            return

        report_text = (command.args or "").strip()
        if not report_text and message.reply_to_message:
            replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
            if replied_text:
                report_text = f"Проблема с сообщением: {replied_text[:1200]}"

        if report_text:
            await save_user_report(message, bot, report_text, lang)
            return

        if user_id:
            pending_problem_reports.add(user_id)
        await message.answer(t(lang, "help_prompt"))

    @router.message(Command("lang"))
    async def language_command(message: Message, command: CommandObject, bot: Bot) -> None:
        lang = await remember_user(message)
        user_id = message.from_user.id if message.from_user else 0
        if not await user_can_use_bot(message, user_id):
            return
        requested = (command.args or "").strip().lower()
        if requested not in SUPPORTED_LANGS:
            await message.answer(t(lang, "unknown_lang"), reply_markup=start_keyboard(lang))
            return
        await storage.set_language(user_id, requested)
        await message.answer(t(requested, "language_set"), reply_markup=start_keyboard(requested))
        await send_interaction_ads(bot, message.chat.id, user_id)

    @router.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return
        stats = await storage.stats()
        await message.answer(t(lang, "stats", **stats))

    @router.message(Command("broadcast_text"))
    async def broadcast_command(message: Message, command: CommandObject, bot: Bot) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return
        text = (command.args or "").strip()
        if not text:
            await message.answer(t(lang, "broadcast_usage"))
            return

        sent = 0
        failed = 0
        for user_id in await storage.all_user_ids():
            try:
                await bot.send_message(user_id, text)
                sent += 1
            except (TelegramBadRequest, TelegramForbiddenError):
                failed += 1
        await message.answer(t(lang, "broadcast_done", sent=sent, failed=failed))

    @router.message(Command("premium"))
    async def premium_command(message: Message, command: CommandObject) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        args = (command.args or "").strip()
        if not args:
            for text in admin_help_messages():
                await message.answer(text)
            return

        parts = args.split()
        if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
            await message.answer(t(lang, "premium_usage"))
            return

        try:
            target_user_id = int(parts[0])
        except ValueError:
            await message.answer(t(lang, "premium_usage"))
            return

        enabled = parts[1].lower() == "on"
        await storage.set_premium(target_user_id, enabled)
        await message.answer(t(lang, "premium_done", user_id=target_user_id, state="on" if enabled else "off"))

    @router.message(Command("admin"))
    async def admin_command(message: Message, command: CommandObject) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        for text in admin_help_messages():
            await message.answer(text)

    @router.message(Command("friend"))
    async def friend_command(message: Message, command: CommandObject) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 2 or parts[1].lower() not in {"on", "off"}:
            await message.answer(t(lang, "friend_usage"))
            return

        try:
            target_user_id = int(parts[0])
        except ValueError:
            await message.answer(t(lang, "friend_usage"))
            return

        enabled = parts[1].lower() == "on"
        note = parts[2] if len(parts) > 2 else None
        await storage.set_friend(target_user_id, enabled, note)
        await message.answer(t(lang, "friend_done", user_id=target_user_id, state="on" if enabled else "off"))

    @router.message(Command("ad"))
    async def ad_command(message: Message, command: CommandObject) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"after_message", "every_8h"}:
            await message.answer(t(lang, "ad_usage"))
            return

        ad_id = await storage.add_ad(parts[0], parts[1])
        await message.answer(t(lang, "ad_done", ad_id=ad_id))

    @router.message(Command("ads"))
    async def ads_command(message: Message) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        ads = await storage.list_ads()
        if not ads:
            await message.answer(t(lang, "ads_empty"))
            return
        lines = [
            f"#{ad['id']} {ad['placement']} {'on' if ad['is_active'] else 'off'}: {html.escape(ad['text'][:80])}"
            for ad in ads
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("required"))
    async def required_command(message: Message, command: CommandObject) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 2 or parts[0] not in {"telegram_channel", "telegram_app", "link"}:
            await message.answer(t(lang, "required_usage"))
            return

        title = parts[2] if len(parts) > 2 else parts[1]
        await storage.add_required_link(parts[0], parts[1], title)
        await message.answer(t(lang, "required_done"))

    @router.message(Command("required_list"))
    async def required_list_command(message: Message) -> None:
        lang = await remember_user(message)
        if not await is_admin(message.from_user.id if message.from_user else None):
            await message.answer(t(lang, "admin_only"))
            return

        links = await storage.active_required_links()
        if not links:
            await message.answer(t(lang, "required_empty"))
            return
        await message.answer("\n".join(display_required_link(link) for link in links))

    @router.callback_query(F.data.startswith("lang:"))
    async def language_callback(callback: CallbackQuery) -> None:
        user = callback.from_user
        requested = (callback.data or "").split(":", 1)[1].strip().lower()
        if requested not in SUPPORTED_LANGS:
            await callback.answer("Unknown language", show_alert=True)
            return

        await storage.upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=pick_lang(user.language_code),
            is_admin=user.id in config.admin_ids,
        )
        await storage.set_language(user.id, requested)
        await callback.answer()

        if callback.message is None:
            return

        text = t(requested, "language_saved", language=language_label(requested, requested))
        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=text, reply_markup=None)
            else:
                await callback.message.edit_text(text, reply_markup=None)
        except TelegramBadRequest:
            await callback.message.answer(text)

    @router.callback_query(F.data.startswith("audio:"))
    async def audio_callback(callback: CallbackQuery, bot: Bot) -> None:
        user = callback.from_user
        lang = await lang_for_user(user.id, user.language_code)
        if callback.message is None:
            await callback.answer()
            return
        if await storage.is_banned(user.id):
            await callback.answer()
            return
        if not await ensure_subscription(bot, user.id, lang):
            await callback.answer()
            return

        track_id = callback.data.split(":", 1)[1] if callback.data else ""
        lock_key = audio_request_key(callback.message.chat.id, user.id, track_id)
        if was_recent_audio_send(lock_key):
            await callback.answer(t(lang, "audio_sent"))
            return
        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await callback.answer(t(lang, "download_already_running"))
            return

        ok = False
        answered = False
        try:
            async with lock:
                await callback.answer()
                answered = True
                if was_recent_audio_send(lock_key):
                    return
                track = await spotify.get_track(track_id)
                await storage.save_track_metadata(track)
                ok = await send_track_audio(bot, callback.message.chat.id, user.id, track, lang)
                if ok:
                    mark_recent_audio_send(lock_key)
        except SpotifyAuthError:
            await storage.record_failed_download(user.id, track_id, "spotify_auth_missing")
            await bot.send_message(callback.message.chat.id, t(lang, "auth_missing"))
        except SpotifyPremiumRequired:
            await storage.record_failed_download(user.id, track_id, "spotify_premium_required")
            await bot.send_message(callback.message.chat.id, t(lang, "spotify_premium_required"))
        except (SpotifyError, SpotifyNotFound):
            await storage.record_failed_download(user.id, track_id, "spotify_error")
            await bot.send_message(callback.message.chat.id, t(lang, "spotify_error"))
        finally:
            audio_download_locks.pop(lock_key, None)
        if not answered:
            await callback.answer(t(lang, "audio_sent") if ok else None)

    @router.callback_query(F.data.startswith("ytm:"))
    async def youtube_music_candidate_callback(callback: CallbackQuery, bot: Bot) -> None:
        user = callback.from_user
        lang = await lang_for_user(user.id, user.language_code)
        if callback.message is None:
            await callback.answer()
            return
        if await storage.is_banned(user.id):
            await callback.answer()
            return
        if not await ensure_subscription(bot, user.id, lang):
            await callback.answer()
            return

        try:
            _, track_id, source_id = (callback.data or "").split(":", 2)
        except ValueError:
            await callback.answer(t(lang, "candidate_expired"), show_alert=True)
            return

        candidate = await storage.get_source_candidate(track_id, source_id)
        if not candidate:
            await callback.answer(t(lang, "candidate_expired"), show_alert=True)
            return

        lock_key = audio_request_key(callback.message.chat.id, user.id, track_id, source_id)
        if was_recent_audio_send(lock_key):
            await callback.answer(t(lang, "audio_sent"))
            return
        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await callback.answer(t(lang, "download_already_running"))
            return

        await callback.answer()
        ok = False
        try:
            async with lock:
                if was_recent_audio_send(lock_key):
                    return
                track = await spotify.get_track(track_id)
                await storage.save_track_metadata(track)
                ok = await send_track_audio(
                    bot,
                    callback.message.chat.id,
                    user.id,
                    track,
                    lang,
                    quiet=False,
                    source_candidate=stored_candidate_payload(candidate),
                    offer_candidates=False,
                )
                if ok:
                    mark_recent_audio_send(lock_key)
        except SpotifyAuthError:
            await storage.record_failed_download(user.id, track_id, "spotify_auth_missing")
            await bot.send_message(callback.message.chat.id, t(lang, "auth_missing"))
        except SpotifyPremiumRequired:
            await storage.record_failed_download(user.id, track_id, "spotify_premium_required")
            await bot.send_message(callback.message.chat.id, t(lang, "spotify_premium_required"))
        except (SpotifyError, SpotifyNotFound):
            await storage.record_failed_download(user.id, track_id, "spotify_error")
            await bot.send_message(callback.message.chat.id, t(lang, "spotify_error"))
        finally:
            audio_download_locks.pop(lock_key, None)

        if not ok and not (audio_provider and audio_provider.enabled):
            await bot.send_message(callback.message.chat.id, t(lang, "authorized_backend_missing"))

    @router.callback_query(F.data.startswith("ytm_cancel:"))
    async def youtube_music_cancel_callback(callback: CallbackQuery) -> None:
        user = callback.from_user
        lang = await lang_for_user(user.id, user.language_code)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await callback.message.answer(t(lang, "candidate_cancelled"))
        await callback.answer()

    @router.callback_query(F.data.startswith("ytmp:"))
    async def youtube_music_playlist_callback(callback: CallbackQuery, bot: Bot) -> None:
        user = callback.from_user
        lang = await lang_for_user(user.id, user.language_code)
        if callback.message is None:
            await callback.answer()
            return
        if await storage.is_banned(user.id):
            await callback.answer()
            return
        if not await ensure_subscription(bot, user.id, lang):
            await callback.answer()
            return

        playlist_id = (callback.data or "").split(":", 1)[1].strip()
        if not playlist_id:
            await callback.answer(t(lang, "candidate_expired"), show_alert=True)
            return

        lock_key = youtube_music_playlist_request_key(callback.message.chat.id, user.id, playlist_id)
        if was_recent_audio_send(lock_key, ttl_seconds=300):
            await callback.answer(t(lang, "download_already_running"))
            return
        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await callback.answer(t(lang, "download_already_running"))
            return

        await callback.answer()
        try:
            async with lock:
                if was_recent_audio_send(lock_key, ttl_seconds=300):
                    return
                try:
                    playlist = await fetch_youtube_music_playlist(playlist_id, user.id)
                except YouTubeMusicPlaylistError as error:
                    await storage.log_error("youtube_music_playlist", f"{playlist_id}: {error}")
                    await bot.send_message(callback.message.chat.id, t(lang, "external_metadata_failed"))
                    return

                await send_youtube_music_playlist_audio(bot, callback.message.chat.id, user.id, playlist, lang)
                mark_recent_audio_send(lock_key)
        finally:
            audio_download_locks.pop(lock_key, None)

    @router.callback_query(F.data.startswith("bundle:"))
    async def bundle_callback(callback: CallbackQuery, bot: Bot) -> None:
        user = callback.from_user
        lang = await lang_for_user(user.id, user.language_code)
        if callback.message is None:
            await callback.answer()
            return
        if await storage.is_banned(user.id):
            await callback.answer()
            return
        if not await ensure_subscription(bot, user.id, lang):
            await callback.answer()
            return

        _, kind, spotify_id = (callback.data or "").split(":", 2)
        lock_key = collection_request_key(callback.message.chat.id, user.id, kind, spotify_id)
        if was_recent_audio_send(lock_key, ttl_seconds=300):
            await callback.answer(t(lang, "download_already_running"))
            return
        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            await callback.answer(t(lang, "download_already_running"))
            return

        await callback.answer()
        try:
            async with lock:
                if was_recent_audio_send(lock_key, ttl_seconds=300):
                    return
                try:
                    collection = await fetch_collection(SpotifyLink(kind=kind, id=spotify_id), user.id)
                    if collection.kind == "album":
                        await storage.save_album_cache(collection)
                except SpotifyAuthError:
                    await storage.record_failed_download(user.id, spotify_id, "spotify_auth_missing")
                    await bot.send_message(callback.message.chat.id, t(lang, "auth_missing"))
                    return
                except SpotifyPremiumRequired:
                    await storage.record_failed_download(user.id, spotify_id, "spotify_premium_required")
                    await bot.send_message(callback.message.chat.id, t(lang, "spotify_premium_required"))
                    return
                except (SpotifyError, SpotifyNotFound):
                    await storage.record_failed_download(user.id, spotify_id, "spotify_error")
                    await bot.send_message(callback.message.chat.id, t(lang, "spotify_error"))
                    return

                await send_collection_audio(bot, callback.message.chat.id, user.id, collection, lang)
                mark_recent_audio_send(lock_key)
        finally:
            audio_download_locks.pop(lock_key, None)

    @router.message(F.text)
    async def link_message(message: Message, bot: Bot) -> None:
        link_text = message.text or ""
        if is_group_chat(message):
            group_payload = group_music_command_payload(message)
            if group_payload is None:
                return
            link_text = group_payload

        lang = await remember_user(message)
        user_id = message.from_user.id if message.from_user else 0
        if user_id in pending_problem_reports:
            text = (message.text or message.caption or "").strip()
            if text:
                pending_problem_reports.discard(user_id)
                await save_user_report(message, bot, text, lang)
                return
        if not await user_can_use_bot(message, user_id):
            return
        if not await ensure_subscription(bot, user_id, lang, message):
            return

        recognized = recognize_link(link_text)
        if recognized.kind == LinkKind.NO_URL:
            await message.answer(t(lang, "unsupported_link"))
            await send_interaction_ads(bot, message.chat.id, user_id)
            return
        if recognized.kind == LinkKind.YOUTUBE_VIDEO:
            await message.answer(t(lang, "youtube_video_link_error"))
            await send_interaction_ads(bot, message.chat.id, user_id)
            return
        if recognized.kind == LinkKind.UNKNOWN_URL:
            await message.answer(t(lang, "unknown_url"))
            await send_interaction_ads(bot, message.chat.id, user_id)
            return

        link = recognized.spotify
        try:
            if recognized.kind == LinkKind.YOUTUBE_MUSIC_TRACK:
                await handle_direct_youtube_music_link(
                    message,
                    bot,
                    user_id,
                    recognized.video_id or "",
                    recognized.url or "",
                    lang,
                )
                await send_interaction_ads(bot, message.chat.id, user_id)
                return

            if recognized.kind == LinkKind.YOUTUBE_MUSIC_PLAYLIST:
                await handle_direct_youtube_music_playlist_link(
                    message,
                    user_id,
                    recognized.playlist_id or "",
                    lang,
                )
                await send_interaction_ads(bot, message.chat.id, user_id)
                return

            if recognized.kind == LinkKind.EXTERNAL_MUSIC:
                await handle_external_music_link(
                    message,
                    bot,
                    user_id,
                    recognized.url or "",
                    lang,
                )
                await send_interaction_ads(bot, message.chat.id, user_id)
                return

            if link is None:
                await message.answer(t(lang, "unknown_url"))
                await send_interaction_ads(bot, message.chat.id, user_id)
                return

            if link.kind == "track":
                loading_message = await send_loading(message)
                try:
                    track = await spotify.get_track(link.id)
                    await storage.save_track_metadata(track)
                    ok = await send_track_audio(
                        bot,
                        message.chat.id,
                        user_id,
                        track,
                        lang,
                        quiet=True,
                        offer_candidates=False,
                    )
                finally:
                    await clear_status(loading_message)
                if not ok:
                    await message.answer(t(lang, "audio_missing"), reply_markup=source_keyboard(track, lang))
            else:
                collection = await fetch_collection(link, user_id)
                if collection.kind == "album":
                    await storage.save_album_cache(collection)
                await send_collection_card(message, collection, lang)
                if link.kind == "album":
                    await send_collection_once(bot, message.chat.id, user_id, collection, lang)
        except SpotifyAuthError:
            await storage.record_failed_download(user_id, link.id if link else "", "spotify_auth_missing")
            await message.answer(t(lang, "auth_missing"))
        except SpotifyPremiumRequired:
            await storage.record_failed_download(user_id, link.id if link else "", "spotify_premium_required")
            await message.answer(t(lang, "spotify_premium_required"))
        except SpotifyNotFound:
            await storage.record_failed_download(user_id, link.id if link else "", "spotify_not_found")
            await message.answer(t(lang, "unsupported_link"))
        except SpotifyError:
            await storage.record_failed_download(user_id, link.id if link else "", "spotify_error")
            await message.answer(t(lang, "spotify_error"))
        await send_interaction_ads(bot, message.chat.id, user_id)

    async def fetch_collection(link: SpotifyLink, user_id: int) -> SpotifyCollection:
        limit = await album_limit_for_user(user_id)
        if link.kind == "album":
            return await spotify.get_album(link.id, limit)
        return await spotify.get_playlist(link.id, limit)

    async def fetch_youtube_music_playlist(playlist_id: str, user_id: int) -> YouTubeMusicPlaylist:
        limit = await album_limit_for_user(user_id)
        return await youtube_music_playlist_resolver.get_playlist(playlist_id, limit)

    async def handle_direct_youtube_music_link(
        message: Message,
        bot: Bot,
        user_id: int,
        video_id: str,
        url: str,
        lang: str,
    ) -> None:
        if not video_id:
            await message.answer(t(lang, "unknown_url"))
            return

        loading_message = await send_loading(message)
        cached_row = await storage.get_cached_track_by_url(url)
        if cached_row:
            track = cached_row_to_track(cached_row, fallback_url=url)
            try:
                ok = await send_track_audio(
                    bot,
                    message.chat.id,
                    user_id,
                    track,
                    lang,
                    quiet=True,
                    offer_candidates=False,
                )
            finally:
                await clear_status(loading_message)
            if not ok:
                await message.answer(t(lang, "audio_missing"), reply_markup=source_keyboard(track, lang))
            return

        try:
            candidate = await youtube_music_matcher.get_track_candidate(video_id)
        except (YouTubeMusicMatcherError, OSError, ValueError) as error:
            await storage.log_error("youtube_music_direct_metadata", str(error))
            candidate = YouTubeMusicCandidate(
                source="youtube_music",
                source_id=video_id,
                title="YouTube Music",
                artist="YouTube Music",
                url=url,
                thumbnail_url=None,
                score=100,
                is_exact=True,
            )

        await storage.save_source_candidates(f"ytm_{video_id}", [candidate])
        track = SpotifyTrack(
            id=f"ytm_{video_id}",
            name=candidate.title,
            artists=(candidate.artist,),
            album="YouTube Music",
            duration_ms=0,
            spotify_url=url,
            image_url=candidate.thumbnail_url,
        )
        await storage.save_track_metadata(track)
        try:
            ok = await send_track_audio(
                bot,
                message.chat.id,
                user_id,
                track,
                lang,
                quiet=True,
                source_candidate=candidate.to_provider_payload(),
                offer_candidates=False,
            )
        finally:
            await clear_status(loading_message)
        if not ok:
            await message.answer(t(lang, "audio_missing"), reply_markup=source_candidate_keyboard(candidate.to_provider_payload(), lang))

    async def handle_direct_youtube_music_playlist_link(
        message: Message,
        user_id: int,
        playlist_id: str,
        lang: str,
    ) -> None:
        if not playlist_id:
            await message.answer(t(lang, "unknown_url"))
            return

        loading_message = await send_loading(message)
        try:
            playlist = await fetch_youtube_music_playlist(playlist_id, user_id)
        except YouTubeMusicPlaylistError as error:
            await storage.log_error("youtube_music_playlist_metadata", f"{playlist_id}: {error}")
            await clear_status(loading_message)
            await message.answer(t(lang, "external_metadata_failed"))
            return
        finally:
            await clear_status(loading_message)

        await send_youtube_music_playlist_card(message, playlist, lang)

    async def handle_external_music_link(
        message: Message,
        bot: Bot,
        user_id: int,
        url: str,
        lang: str,
    ) -> None:
        if not url:
            await message.answer(t(lang, "unknown_url"))
            return

        loading_message = await send_loading(message)
        cached_row = await storage.get_cached_track_by_url(url)
        if cached_row:
            track = cached_row_to_track(cached_row, fallback_url=url)
            try:
                ok = await send_track_audio(
                    bot,
                    message.chat.id,
                    user_id,
                    track,
                    lang,
                    quiet=True,
                    offer_candidates=False,
                )
            finally:
                await clear_status(loading_message)
            if not ok:
                await message.answer(t(lang, "audio_missing"), reply_markup=source_keyboard(track, lang))
            return

        try:
            external_track = await external_music_resolver.resolve(url)
        except (ExternalMusicResolverError, OSError, ValueError) as error:
            await storage.log_error("external_music_metadata", f"{url}: {error}")
            await clear_status(loading_message)
            await message.answer(t(lang, "external_metadata_failed"))
            return

        track = SpotifyTrack(
            id=f"ext_{external_track.source_id}",
            name=external_track.title,
            artists=(external_track.artist,),
            album=external_track.source_name,
            duration_ms=0,
            spotify_url=url,
            image_url=external_track.image_url,
        )
        await storage.save_track_metadata(track)
        try:
            ok = await send_track_audio(
                bot,
                message.chat.id,
                user_id,
                track,
                lang,
                quiet=True,
                source_candidate=external_track.to_source_candidate(),
                offer_candidates=False,
            )
        finally:
            await clear_status(loading_message)
        if not ok:
            await message.answer(t(lang, "audio_missing"), reply_markup=source_keyboard(track, lang))

    async def album_limit_for_user(user_id: int) -> int:
        if user_id in config.admin_ids or await storage.is_premium_user(user_id):
            return config.premium_album_track_limit
        return config.free_album_track_limit

    async def send_track_card(message: Message, track: SpotifyTrack, lang: str) -> None:
        caption = track_caption(track, lang)
        keyboard = track_keyboard(track, lang)
        if track.image_url:
            await message.answer_photo(track.image_url, caption=caption, reply_markup=keyboard)
        else:
            await message.answer(caption, reply_markup=keyboard)

    async def send_collection_card(message: Message, collection: SpotifyCollection, lang: str) -> None:
        caption = collection_caption(collection, lang)
        keyboard = collection_keyboard(collection, lang)
        if collection.image_url:
            await message.answer_photo(collection.image_url, caption=caption, reply_markup=keyboard)
        else:
            await message.answer(caption, reply_markup=keyboard)

    async def send_youtube_music_playlist_card(
        message: Message,
        playlist: YouTubeMusicPlaylist,
        lang: str,
    ) -> None:
        collection = playlist.to_collection()
        caption = collection_caption(collection, lang)
        keyboard = youtube_music_playlist_keyboard(playlist, lang)
        if playlist.image_url:
            try:
                await message.answer_photo(playlist.image_url, caption=caption, reply_markup=keyboard)
                return
            except TelegramBadRequest:
                pass
        await message.answer(caption, reply_markup=keyboard)

    async def send_collection_audio(
        bot: Bot,
        chat_id: int,
        user_id: int,
        collection: SpotifyCollection,
        lang: str,
    ) -> None:
        await send_message_with_retry(bot, chat_id, t(lang, "bundle_started", limit=len(collection.tracks)))
        sent = 0
        for index, track in enumerate(collection.tracks):
            include_album = collection.kind == "album" and index == len(collection.tracks) - 1
            try:
                ok = await send_track_audio(
                    bot,
                    chat_id,
                    user_id,
                    track,
                    lang,
                    quiet=True,
                    include_album=include_album,
                    offer_candidates=False,
                )
            except Exception as error:
                await storage.log_error(
                    "collection_track_send",
                    f"{collection.kind}:{collection.id} #{index + 1} {track.artist_names} - {track.name}: {error}",
                    traceback.format_exc(),
                )
                await storage.record_failed_download(
                    user_id,
                    track.id or f"{collection.id}:{index + 1}",
                    "collection_track_send_failed",
                )
                continue
            if ok:
                sent += 1
        await send_message_with_retry(bot, chat_id, t(lang, "bundle_done", sent=sent, total=len(collection.tracks)))

    async def send_youtube_music_playlist_audio(
        bot: Bot,
        chat_id: int,
        user_id: int,
        playlist: YouTubeMusicPlaylist,
        lang: str,
    ) -> None:
        await send_message_with_retry(bot, chat_id, t(lang, "bundle_started", limit=len(playlist.tracks)))
        sent = 0
        for index, item in enumerate(playlist.tracks):
            track = item.to_spotify_track(playlist.name)
            await storage.save_track_metadata(track)
            try:
                ok = await send_track_audio(
                    bot,
                    chat_id,
                    user_id,
                    track,
                    lang,
                    quiet=True,
                    source_candidate=item.to_source_candidate(),
                    offer_candidates=False,
                )
            except Exception as error:
                await storage.log_error(
                    "youtube_music_playlist_track_send",
                    f"{playlist.playlist_id} #{index + 1} {item.artist_names} - {item.title}: {error}",
                    traceback.format_exc(),
                )
                await storage.record_failed_download(
                    user_id,
                    track.id or f"{playlist.playlist_id}:{index + 1}",
                    "youtube_music_playlist_track_send_failed",
                )
                continue
            if ok:
                sent += 1
        await send_message_with_retry(bot, chat_id, t(lang, "bundle_done", sent=sent, total=len(playlist.tracks)))

    async def send_collection_once(
        bot: Bot,
        chat_id: int,
        user_id: int,
        collection: SpotifyCollection,
        lang: str,
    ) -> bool:
        lock_key = collection_request_key(chat_id, user_id, collection.kind, collection.id)
        if was_recent_audio_send(lock_key, ttl_seconds=300):
            return False

        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            return False

        try:
            async with lock:
                if was_recent_audio_send(lock_key, ttl_seconds=300):
                    return False
                await send_collection_audio(bot, chat_id, user_id, collection, lang)
                mark_recent_audio_send(lock_key)
                return True
        finally:
            audio_download_locks.pop(lock_key, None)

    async def send_track_audio(
        bot: Bot,
        chat_id: int,
        user_id: int,
        track: SpotifyTrack,
        lang: str,
        quiet: bool = False,
        include_album: bool = False,
        source_candidate: dict[str, object] | None = None,
        offer_candidates: bool = True,
    ) -> bool:
        lock_key = track_send_key(chat_id, user_id, track)
        if was_recent_audio_send(lock_key):
            return True

        lock = audio_download_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            return True

        try:
            async with lock:
                if was_recent_audio_send(lock_key):
                    return True
                ok = await _send_track_audio_unlocked(
                    bot,
                    chat_id,
                    user_id,
                    track,
                    lang,
                    quiet=quiet,
                    include_album=include_album,
                    source_candidate=source_candidate,
                    offer_candidates=offer_candidates,
                )
                if ok:
                    mark_recent_audio_send(lock_key)
                return ok
        finally:
            audio_download_locks.pop(lock_key, None)

    async def _send_track_audio_unlocked(
        bot: Bot,
        chat_id: int,
        user_id: int,
        track: SpotifyTrack,
        lang: str,
        quiet: bool = False,
        include_album: bool = False,
        source_candidate: dict[str, object] | None = None,
        offer_candidates: bool = True,
    ) -> bool:
        status_message: Message | None = None
        cached_audio = await storage.get_track_audio_cache(track.id, track)
        if cached_audio and cached_audio.get("cache_key") and cached_audio.get("file_id"):
            cache_key = str(cached_audio["cache_key"])
            cached_source = str(cached_audio.get("source") or "track_cache")
            if "cache" not in cached_source.lower():
                cached_source = f"track_cache:{cached_source}"
            if not await storage.get_cached_file(cache_key):
                await storage.save_cached_file(
                    cache_key,
                    str(cached_audio["file_id"]),
                    cached_audio.get("file_unique_id"),
                    "audio",
                )
            await send_audio_file(
                bot,
                chat_id,
                user_id,
                track,
                lang,
                None,
                cache_key,
                cached_source,
                include_album=include_album,
            )
            return True

        local_file = library.find_track(track)
        if local_file:
            await send_audio_file(
                bot,
                chat_id,
                user_id,
                track,
                lang,
                local_file,
                f"local:{track.id}",
                "local",
                include_album=include_album,
            )
            return True

        if audio_provider and audio_provider.enabled:
            provider_candidate = source_candidate or youtube_music_search_candidate(track)
            if provider_candidate is not None:
                cache_key = provider_cache_key(track, provider_candidate)
                cached_file_id = await storage.get_cached_file(cache_key)
                if cached_file_id:
                    await send_audio_file(
                        bot,
                        chat_id,
                        user_id,
                        track,
                        lang,
                        None,
                        cache_key,
                        "authorized_api_cache",
                        include_album=include_album,
                    )
                    return True
            if provider_candidate is not None and not quiet:
                status_message = await send_status(bot, chat_id, lang, "download_status_fetching")
            try:
                authorized_audio = await audio_provider.fetch(track, source_candidate=provider_candidate)
            except AuthorizedAudioProviderError as error:
                await clear_status(status_message)
                await storage.log_error("authorized_audio", str(error))
                await notify_admins(
                    bot,
                    backend_alert_text("Audio backend failed", track, str(error)),
                )
                authorized_audio = None
            if authorized_audio:
                if authorized_audio.failover_warnings:
                    await notify_admins(
                        bot,
                        backend_alert_text(
                            f"Audio backend failover succeeded on #{authorized_audio.provider_index}",
                            track,
                            "; ".join(authorized_audio.failover_warnings),
                        ),
                    )
                await clear_status(status_message)
                await send_audio_file(
                    bot,
                    chat_id,
                    user_id,
                    track,
                    lang,
                    authorized_audio.path,
                    authorized_audio.cache_key,
                    f"authorized_api:{authorized_audio.provider_index}",
                    include_album=include_album,
                )
                return True

        if source_candidate is not None:
            await clear_status(status_message)
            if not quiet:
                await bot.send_message(
                    chat_id,
                    t(lang, "candidate_audio_missing"),
                    reply_markup=source_candidate_keyboard(source_candidate, lang),
                )
            await storage.record_failed_download(user_id, track.id, "source_candidate_audio_missing")
            return False

        if offer_candidates:
            candidate_result = await offer_youtube_music_candidates(bot, chat_id, user_id, track, lang)
            if candidate_result == "sent":
                return True
            if candidate_result == "shown":
                return False

        if not quiet:
            await bot.send_message(chat_id, t(lang, "audio_missing"), reply_markup=source_keyboard(track, lang))
        await storage.record_failed_download(user_id, track.id, "audio_missing")
        return False

    async def offer_youtube_music_candidates(
        bot: Bot,
        chat_id: int,
        user_id: int,
        track: SpotifyTrack,
        lang: str,
    ) -> str:
        if not youtube_music_matcher.enabled:
            return "none"

        status_message = await send_status(bot, chat_id, lang, "download_status_searching")
        try:
            candidates = await youtube_music_matcher.search(track, limit=3)
        except (YouTubeMusicMatcherError, OSError, ValueError) as error:
            await update_status(status_message, lang, "download_status_search_failed")
            await storage.log_error("youtube_music_search", str(error))
            await storage.record_failed_download(user_id, track.id, "youtube_music_search_failed")
            return "none"

        if not candidates:
            await clear_status(status_message)
            return "none"

        await storage.save_source_candidates(track.id, candidates)

        exact = next((candidate for candidate in candidates if candidate.is_exact), None)
        if exact and audio_provider and audio_provider.enabled:
            await update_status(status_message, lang, "download_status_exact")
            try:
                authorized_audio = await audio_provider.fetch(track, source_candidate=exact.to_provider_payload())
            except AuthorizedAudioProviderError as error:
                await update_status(status_message, lang, "download_status_backend_failed")
                await storage.log_error("authorized_audio_youtube_music", str(error))
                await notify_admins(
                    bot,
                    backend_alert_text("YouTube Music audio backend failed", track, str(error)),
                )
                authorized_audio = None
            if authorized_audio:
                if authorized_audio.failover_warnings:
                    await notify_admins(
                        bot,
                        backend_alert_text(
                            f"YouTube Music backend failover succeeded on #{authorized_audio.provider_index}",
                            track,
                            "; ".join(authorized_audio.failover_warnings),
                        ),
                    )
                await clear_status(status_message)
                await send_audio_file(
                    bot,
                    chat_id,
                    user_id,
                    track,
                    lang,
                    authorized_audio.path,
                    authorized_audio.cache_key,
                    f"authorized_api:youtube_music:{authorized_audio.provider_index}",
                )
                return "sent"

        await send_youtube_music_candidates(bot, chat_id, track, lang, candidates, status_message)
        await storage.record_failed_download(
            user_id,
            track.id,
            "youtube_music_candidates_shown" if not exact else "youtube_music_exact_backend_missing",
        )
        return "shown"

    async def send_youtube_music_candidates(
        bot: Bot,
        chat_id: int,
        track: SpotifyTrack,
        lang: str,
        candidates: list[YouTubeMusicCandidate],
        intro_message: Message | None = None,
    ) -> None:
        if intro_message is not None:
            await update_status(intro_message, lang, "youtube_candidates_intro")
        else:
            await bot.send_message(chat_id, t(lang, "youtube_candidates_intro"))
        for index, candidate in enumerate(candidates[:3], start=1):
            caption = candidate_caption(track, candidate, index, lang)
            keyboard = candidate_keyboard(track, candidate, index, lang)
            try:
                if candidate.thumbnail_url:
                    await bot.send_photo(
                        chat_id,
                        candidate.thumbnail_url,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(chat_id, caption, reply_markup=keyboard)
            except TelegramBadRequest:
                await bot.send_message(chat_id, caption, reply_markup=keyboard)

    async def send_audio_file(
        bot: Bot,
        chat_id: int,
        user_id: int,
        track: SpotifyTrack,
        lang: str,
        path: Path | None,
        cache_key: str,
        source: str,
        include_album: bool = False,
    ) -> None:
        cached_file_id = await storage.get_cached_file(cache_key)
        history_source = source
        if cached_file_id and "cache" not in history_source.lower():
            history_source = f"file_cache:{history_source}"
        caption = track_caption(track, lang, include_album=include_album)
        if cached_file_id:
            sent_message = await send_audio_with_retry(bot, chat_id, cached_file_id, caption=caption)
            await storage.mark_track_audio_cached(track, cache_key, cached_file_id, None, history_source, path)
        else:
            if path is None:
                raise RuntimeError("audio path is required when Telegram file_id cache is empty")
            thumbnail = await track_thumbnail(track)
            sent_message = await send_audio_with_retry(
                bot,
                chat_id,
                FSInputFile(path),
                title=track.name[:64],
                performer=track.artist_names[:64],
                caption=caption,
                thumbnail=thumbnail,
            )
            if sent_message.audio:
                await storage.save_cached_file(
                    cache_key=cache_key,
                    file_id=sent_message.audio.file_id,
                    file_unique_id=sent_message.audio.file_unique_id,
                    media_type="audio",
                )
                await storage.mark_track_audio_cached(
                    track,
                    cache_key,
                    sent_message.audio.file_id,
                    sent_message.audio.file_unique_id,
                    history_source,
                    path,
                )

        count = await storage.record_download(user_id, track.id, history_source)
        if await should_show_ad(user_id, count):
            await bot.send_message(chat_id, f"<b>{html.escape(t(lang, 'ad_prefix'))}</b>\n{html.escape(config.ad_text)}")
        if not await storage.is_ad_free_user(user_id):
            for ad in await storage.active_ads_for_user(user_id, "after_message"):
                try:
                    await send_stored_ad(bot, chat_id, ad)
                    await storage.record_ad_sent(user_id, int(ad["id"]), "after_message")
                except (TelegramBadRequest, TelegramForbiddenError) as error:
                    await storage.log_error("ad_after_message", str(error))

    async def should_show_ad(user_id: int, downloads_count: int) -> bool:
        if await storage.is_ad_free_user(user_id):
            return False
        return (
            config.ad_every_n > 0
            and bool(config.ad_text)
            and user_id not in config.no_ads_user_ids
            and user_id not in config.admin_ids
            and downloads_count % config.ad_every_n == 0
        )

    async def send_interaction_ads(bot: Bot, chat_id: int, user_id: int) -> None:
        if await storage.is_ad_free_user(user_id):
            return
        for placement in ("after_message", "every_8h"):
            for ad in await storage.active_ads_for_user(user_id, placement):
                try:
                    await send_stored_ad(bot, chat_id, ad)
                    await storage.record_ad_sent(user_id, int(ad["id"]), placement)
                except (TelegramBadRequest, TelegramForbiddenError) as error:
                    await storage.log_error(f"ad_{placement}", str(error))

    async def send_loading(message: Message) -> Message | None:
        try:
            return await message.answer(custom_emoji("hourglass"))
        except (TelegramBadRequest, TelegramForbiddenError):
            return None

    async def send_status(bot: Bot, chat_id: int, lang: str, key: str) -> Message | None:
        try:
            return await bot.send_message(chat_id, t(lang, key), disable_web_page_preview=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            return None

    async def update_status(message: Message | None, lang: str, key: str) -> None:
        if message is None:
            return
        try:
            await message.edit_text(t(lang, key), disable_web_page_preview=True)
        except TelegramBadRequest:
            pass

    async def clear_status(message: Message | None) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    async def notify_admins(bot: Bot, text: str) -> None:
        now = time.time()
        cache_key = text[:1500]
        if now - admin_alert_cache.get(cache_key, 0) < 300:
            return
        admin_alert_cache[cache_key] = now
        admin_ids = set(config.admin_ids)
        admin_ids.update(await storage.admin_user_ids())
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except (TelegramBadRequest, TelegramForbiddenError):
                continue

    def backend_alert_text(title: str, track: SpotifyTrack, details: str) -> str:
        safe_details = html.escape(details[:1200])
        return (
            f"<b>{html.escape(title)}</b>\n"
            f"Track: {html.escape(track.artist_names)} - {html.escape(track.name)}\n"
            f"Spotify ID: <code>{html.escape(track.id)}</code>\n"
            f"{safe_details}"
        )

    def candidate_keyboard(
        track: SpotifyTrack,
        candidate: YouTubeMusicCandidate,
        index: int,
        lang: str,
    ) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t(lang, "choose_candidate", index=index),
            callback_data=f"ytm:{track.id}:{candidate.source_id}",
            icon_custom_emoji_id=emoji_id("check"),
        )
        builder.button(text=t(lang, "open_youtube_music"), url=candidate.url, icon_custom_emoji_id=emoji_id("youtube_music"))
        builder.button(
            text=t(lang, "cancel_candidates"),
            callback_data=f"ytm_cancel:{track.id}",
            icon_custom_emoji_id=emoji_id("warning"),
        )
        builder.adjust(1)
        return builder.as_markup()

    def source_candidate_keyboard(candidate: dict[str, object], lang: str) -> InlineKeyboardMarkup | None:
        url = str(candidate.get("url") or "")
        if not url:
            return None
        builder = InlineKeyboardBuilder()
        builder.button(text=t(lang, "open_youtube_music"), url=url, icon_custom_emoji_id=emoji_id("youtube_music"))
        builder.adjust(1)
        return builder.as_markup()

    def stored_candidate_payload(candidate: dict[str, object]) -> dict[str, object]:
        return {
            "source": str(candidate.get("source") or "youtube_music"),
            "source_id": str(candidate.get("source_id") or ""),
            "title": str(candidate.get("title") or ""),
            "artist": str(candidate.get("artist") or ""),
            "url": str(candidate.get("url") or ""),
            "thumbnail_url": candidate.get("thumbnail_url"),
            "score": int(candidate.get("score") or 0),
            "is_exact": bool(candidate.get("is_exact")),
        }

    def youtube_music_search_candidate(track: SpotifyTrack) -> dict[str, object]:
        return {
            "source": "youtube_music_search",
            "source_id": track.id,
            "title": track.name,
            "artist": track.artist_names,
            "url": youtube_music_search_url(track),
            "thumbnail_url": track.image_url,
            "score": 0,
            "is_exact": False,
            "search_query": f"{track.artist_names} {track.name}".strip(),
        }

    def track_keyboard(track: SpotifyTrack, lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text=t(lang, "send_audio"), callback_data=f"audio:{track.id}", icon_custom_emoji_id=emoji_id("download"))
        builder.button(text=t(lang, "search_sources"), url=legal_source_links(track)[0].url, icon_custom_emoji_id=emoji_id("link"))
        if track.spotify_url:
            builder.button(text=t(lang, "open_spotify"), url=track.spotify_url, icon_custom_emoji_id=emoji_id("spotify"))
        builder.adjust(1)
        return builder.as_markup()

    def source_icon(name: str) -> str | None:
        lowered = name.casefold()
        if "youtube" in lowered:
            return emoji_id("youtube_music")
        if "spotify" in lowered:
            return emoji_id("spotify")
        if "soundcloud" in lowered or "bandcamp" in lowered or "jamendo" in lowered:
            return emoji_id("music")
        return emoji_id("link")

    def source_keyboard(track: SpotifyTrack, lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for source in legal_source_links(track):
            builder.button(text=source.name, url=source.url, icon_custom_emoji_id=source_icon(source.name))
        if track.spotify_url:
            builder.button(text=t(lang, "open_spotify"), url=track.spotify_url, icon_custom_emoji_id=emoji_id("spotify"))
        builder.adjust(2, 2, 2, 1)
        return builder.as_markup()

    def collection_keyboard(collection: SpotifyCollection, lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t(lang, "send_bundle"),
            callback_data=f"bundle:{collection.kind}:{collection.id}",
            icon_custom_emoji_id=emoji_id("download"),
        )
        if collection.spotify_url:
            builder.button(text=t(lang, "open_spotify"), url=collection.spotify_url, icon_custom_emoji_id=emoji_id("spotify"))
        builder.adjust(1)
        return builder.as_markup()

    def youtube_music_playlist_keyboard(playlist: YouTubeMusicPlaylist, lang: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t(lang, "send_bundle"),
            callback_data=f"ytmp:{playlist.playlist_id}",
            icon_custom_emoji_id=emoji_id("download"),
        )
        builder.button(
            text=t(lang, "open_youtube_music"),
            url=playlist.url,
            icon_custom_emoji_id=emoji_id("youtube_music"),
        )
        builder.adjust(1)
        return builder.as_markup()

    async def track_thumbnail(track: SpotifyTrack) -> FSInputFile | None:
        if not track.image_url:
            return None

        destination = config.cover_cache_dir / f"{track.id}.jpg"
        if destination.exists():
            return FSInputFile(destination)

        try:
            async with spotify.session.get(track.image_url) as response:
                if response.status >= 400:
                    return None
                image_bytes = await response.read()
            prepared = prepare_audio_thumbnail(image_bytes, destination)
        except Exception:
            return None

        return FSInputFile(prepared)

    def track_caption(track: SpotifyTrack, lang: str, include_album: bool = False) -> str:
        key = "track_caption_with_album" if include_album else "track_caption"
        return t(
            lang,
            key,
            title=html.escape(track.name),
            artists=html.escape(track.artist_names),
            album=html.escape(track.album or "-"),
            signature=signature(),
        )

    def candidate_caption(
        track: SpotifyTrack,
        candidate: YouTubeMusicCandidate,
        index: int,
        lang: str,
    ) -> str:
        match_label = t(lang, "candidate_exact") if candidate.is_exact else t(lang, "candidate_possible")
        return t(
            lang,
            "youtube_candidate_caption",
            index=index,
            title=html.escape(candidate.title),
            artist=html.escape(candidate.artist),
            spotify_title=html.escape(track.name),
            spotify_artists=html.escape(track.artist_names),
            score=candidate.score,
            match=match_label,
            signature=signature(),
        )

    def collection_caption(collection: SpotifyCollection, lang: str) -> str:
        lines = []
        for index, track in enumerate(collection.tracks[:10], start=1):
            lines.append(f"{index}. {html.escape(track.artist_names)} - {html.escape(track.name)}")
        if collection.total > len(lines):
            lines.append("...")

        caption = t(
            lang,
            "collection_caption",
            kind=t(lang, collection.kind),
            name=html.escape(collection.name),
            total=collection.total,
            limit=len(collection.tracks),
            tracks="\n".join(lines),
        )
        if collection.kind == "album" and collection.total > len(collection.tracks):
            caption = f"{caption}\n\n{t(lang, 'album_limit_notice', limit=config.free_album_track_limit)}"
        return caption

    def signature() -> str:
        return f"{custom_emoji('double_heart')} {html.escape(config.bot_public_name)}"

    def display_required_link(required: dict[str, object]) -> str:
        title = str(required.get("title") or required.get("value") or "")
        value = str(required.get("value") or "")
        if title == value:
            return title
        return f"{title} ({value})"

    async def user_can_use_bot(message: Message, user_id: int) -> bool:
        if user_id and await storage.is_banned(user_id):
            return False
        if user_id and await storage.is_admin_user(user_id):
            return True
        if await storage.is_maintenance_on():
            text = await storage.get_setting("maintenance_text")
            await message.answer(text or t(await lang_for_user(user_id), "maintenance_default"))
            return False
        return True

    dispatcher = Dispatcher()
    dispatcher.include_router(create_admin_router(config, storage))
    dispatcher.include_router(router)
    return dispatcher
