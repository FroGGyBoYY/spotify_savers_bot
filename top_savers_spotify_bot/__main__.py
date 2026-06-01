import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

from .admin_panel import send_stored_ad
from .audio_provider import AuthorizedAudioProvider
from .bot import build_dispatcher
from .config import Config
from .library import AudioLibrary
from .spotify import SpotifyClient
from .storage import Storage


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
    config.ensure_dirs()

    storage = Storage(config.database_path)
    await storage.connect()
    await storage.init()
    await storage.sync_admins(config.admin_ids)
    await storage.sync_friends(config.no_ads_user_ids)
    await storage.sync_required_channels(config.required_channels)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await ensure_expected_bot(bot, config)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="add_to_group", description="Добавить бота в группу"),
            BotCommand(command="help", description="Сообщить о проблеме"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="music", description="Скачать музыку по ссылке"),
            BotCommand(command="song", description="Скачать трек по ссылке"),
            BotCommand(command="track", description="Скачать трек по ссылке"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    library = AudioLibrary(config.audio_library_dir)

    try:
        async with SpotifyClient(config) as spotify:
            async with AuthorizedAudioProvider(config) as audio_provider:
                dispatcher = build_dispatcher(config, storage, spotify, library, audio_provider)
                ads_task = asyncio.create_task(run_periodic_ads(bot, storage))
                try:
                    await dispatcher.start_polling(bot)
                finally:
                    ads_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await ads_task
    finally:
        await storage.close()
        await bot.session.close()


async def run_periodic_ads(bot: Bot, storage: Storage) -> None:
    while True:
        for user_id in await storage.all_user_ids():
            if await storage.is_ad_free_user(user_id):
                continue
            for ad in await storage.active_ads_for_user(user_id, "every_8h"):
                try:
                    await send_stored_ad(bot, user_id, ad)
                except (TelegramBadRequest, TelegramForbiddenError):
                    continue
                await storage.record_ad_sent(user_id, int(ad["id"]), "every_8h")
        await asyncio.sleep(600)


async def ensure_expected_bot(bot: Bot, config: Config) -> None:
    expected = config.expected_bot_username.strip().lstrip("@").lower()
    if not expected:
        return

    me = await bot.get_me()
    actual = (me.username or "").lower()
    if actual != expected:
        raise RuntimeError(
            f"BOT_TOKEN belongs to @{actual}, expected @{expected}. "
            "Put the @spotify_savers_bot token into .env."
        )


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot stopped by user.")
