from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _int_set(value: str | None) -> set[int]:
    result: set[int] = set()
    for item in _csv(value):
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _signature_emoji() -> str:
    value = os.getenv("BOT_SIGNATURE_EMOJI", "\U0001F495").strip() or "\U0001F495"
    if value == "рџ’•":
        return "\U0001F495"
    return value


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    expected_bot_username: str
    spotify_client_id: str
    spotify_client_secret: str
    spotify_user_refresh_token: str
    admin_ids: set[int]
    required_channels: list[str]
    ad_every_n: int
    ad_text: str
    no_ads_user_ids: set[int]
    database_path: Path
    audio_library_dir: Path
    cover_cache_dir: Path
    bot_public_name: str
    bot_signature_emoji: str
    authorized_audio_api_url: str
    authorized_audio_api_urls: tuple[str, ...]
    authorized_audio_api_token: str
    authorized_audio_timeout_seconds: int
    youtube_api_key: str
    youtube_region_code: str
    youtube_music_search_enabled: bool
    vk_access_token: str
    vk_browser_metadata_enabled: bool
    vk_browser_metadata_timeout_seconds: int
    free_album_track_limit: int
    premium_album_track_limit: int
    max_playlist_tracks: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(override=True, encoding="utf-8-sig")

        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required. Put it into .env or environment.")

        return cls(
            bot_token=bot_token,
            expected_bot_username=os.getenv("EXPECTED_BOT_USERNAME", "@spotify_savers_bot").strip(),
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
            spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
            spotify_user_refresh_token=(
                os.getenv("SPOTIFY_USER_REFRESH_TOKEN", "").strip()
                or os.getenv("SPOTIFY_REFRESH_TOKEN", "").strip()
            ),
            admin_ids=_int_set(os.getenv("ADMIN_IDS")),
            required_channels=_csv(os.getenv("REQUIRED_CHANNELS")),
            ad_every_n=max(0, _int_env("AD_EVERY_N", 0)),
            ad_text=os.getenv("AD_TEXT", "").strip(),
            no_ads_user_ids=_int_set(os.getenv("NO_ADS_USER_IDS")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            audio_library_dir=Path(os.getenv("AUDIO_LIBRARY_DIR", "library")),
            cover_cache_dir=Path(os.getenv("COVER_CACHE_DIR", "data/covers")),
            bot_public_name=os.getenv("BOT_PUBLIC_NAME", "@spotify_savers_bot").strip() or "@spotify_savers_bot",
            bot_signature_emoji=_signature_emoji(),
            authorized_audio_api_url=os.getenv("AUTHORIZED_AUDIO_API_URL", "").strip(),
            authorized_audio_api_urls=tuple(
                _csv(os.getenv("AUTHORIZED_AUDIO_API_URLS"))
                or _csv(os.getenv("AUTHORIZED_AUDIO_API_URL"))
            ),
            authorized_audio_api_token=os.getenv("AUTHORIZED_AUDIO_API_TOKEN", "").strip(),
            authorized_audio_timeout_seconds=max(5, _int_env("AUTHORIZED_AUDIO_TIMEOUT_SECONDS", 60)),
            youtube_api_key=os.getenv("YOUTUBE_API_KEY", "").strip(),
            youtube_region_code=os.getenv("YOUTUBE_REGION_CODE", "US").strip().upper() or "US",
            youtube_music_search_enabled=os.getenv("YOUTUBE_MUSIC_SEARCH_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
            vk_access_token=(
                os.getenv("VK_ACCESS_TOKEN", "").strip()
                or os.getenv("VK_API_TOKEN", "").strip()
                or os.getenv("VK_SERVICE_TOKEN", "").strip()
            ),
            vk_browser_metadata_enabled=_bool_env("VK_BROWSER_METADATA_ENABLED", False),
            vk_browser_metadata_timeout_seconds=max(5, _int_env("VK_BROWSER_METADATA_TIMEOUT_SECONDS", 18)),
            free_album_track_limit=max(1, _int_env("FREE_ALBUM_TRACK_LIMIT", 50)),
            premium_album_track_limit=max(1, _int_env("PREMIUM_ALBUM_TRACK_LIMIT", 500)),
            max_playlist_tracks=max(1, _int_env("MAX_PLAYLIST_TRACKS", 50)),
        )

    def ensure_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_library_dir.mkdir(parents=True, exist_ok=True)
        self.cover_cache_dir.mkdir(parents=True, exist_ok=True)
