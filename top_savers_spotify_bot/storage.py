from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from .cache_keys import canonical_source_alias_key, canonical_track_key, canonical_url_alias_key


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage is not connected")
        return self._db

    async def init(self) -> None:
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                subscription_status TEXT NOT NULL DEFAULT 'free',
                subscription_until TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_premium INTEGER NOT NULL DEFAULT 0,
                is_friend INTEGER NOT NULL DEFAULT 0,
                downloads_count INTEGER NOT NULL DEFAULT 0,
                last_download_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS file_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                media_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                object_id TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_counters (
                user_id INTEGER PRIMARY KEY,
                downloads_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cached_tracks (
                track_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artists_json TEXT NOT NULL,
                album TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                spotify_url TEXT,
                image_url TEXT,
                audio_cache_key TEXT,
                file_id TEXT,
                file_unique_id TEXT,
                source TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS cached_albums (
                album_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                total_tracks INTEGER NOT NULL DEFAULT 0,
                spotify_url TEXT,
                image_url TEXT,
                tracks_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS track_audio_cache (
                track_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artists_json TEXT NOT NULL,
                artist TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                source_url TEXT,
                image_url TEXT,
                audio_cache_key TEXT,
                file_id TEXT,
                file_unique_id TEXT,
                source TEXT,
                local_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS track_aliases (
                alias_key TEXT PRIMARY KEY,
                track_key TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT,
                source_url TEXT,
                title TEXT,
                artist TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            );

            CREATE TABLE IF NOT EXISTS source_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT,
                url TEXT NOT NULL,
                thumbnail_url TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                is_exact INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT,
                UNIQUE(track_id, source, source_id)
            );

            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                placement TEXT NOT NULL,
                text TEXT NOT NULL,
                media_file_id TEXT,
                media_type TEXT,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                buttons_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                is_active INTEGER NOT NULL DEFAULT 1,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ad_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ad_id INTEGER NOT NULL,
                placement TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS required_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                value TEXT NOT NULL UNIQUE,
                title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS friends (
                user_id INTEGER PRIMARY KEY,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                user_id INTEGER PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS failed_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                object_id TEXT,
                platform TEXT NOT NULL DEFAULT 'spotify',
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS bot_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                message TEXT NOT NULL,
                traceback TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'OPEN'
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_downloads_user_id ON downloads(user_id);
            CREATE INDEX IF NOT EXISTS idx_ads_placement ON ads(placement, is_active);
            CREATE INDEX IF NOT EXISTS idx_ad_delivery_user_ad ON ad_delivery_log(user_id, ad_id, sent_at);
            CREATE INDEX IF NOT EXISTS idx_required_links_active ON required_links(is_active);
            CREATE INDEX IF NOT EXISTS idx_failed_downloads_created ON failed_downloads(created_at);
            CREATE INDEX IF NOT EXISTS idx_bot_errors_created ON bot_errors(created_at);
            CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);
            CREATE INDEX IF NOT EXISTS idx_source_candidates_track ON source_candidates(track_id, source, score);
            CREATE INDEX IF NOT EXISTS idx_track_aliases_track_key ON track_aliases(track_key);
            CREATE INDEX IF NOT EXISTS idx_track_audio_cache_file ON track_audio_cache(file_id);
            """
        )
        await self._ensure_column("users", "last_name", "TEXT")
        await self._ensure_column("users", "subscription_status", "TEXT NOT NULL DEFAULT 'free'")
        await self._ensure_column("users", "subscription_until", "TEXT")
        await self._ensure_column("users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("users", "is_premium", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("users", "is_friend", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("users", "downloads_count", "INTEGER NOT NULL DEFAULT 0")
        await self._ensure_column("users", "last_download_at", "TEXT")
        await self._ensure_column("ads", "media_type", "TEXT")
        await self._ensure_column("ads", "source_chat_id", "INTEGER")
        await self._ensure_column("ads", "source_message_id", "INTEGER")
        await self._ensure_column("ads", "buttons_json", "TEXT NOT NULL DEFAULT '[]'")
        await self._ensure_column("ads", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        await self._ensure_column("ads", "is_deleted", "INTEGER NOT NULL DEFAULT 0")
        await self._backfill_track_audio_cache()
        await self.db.commit()

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cursor = await self.db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        if column not in {str(row["name"]) for row in rows}:
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _backfill_track_audio_cache(self) -> None:
        cursor = await self.db.execute(
            """
            SELECT track_id, title, artists_json, duration_ms, spotify_url, image_url,
                   audio_cache_key, file_id, file_unique_id, source, local_path
            FROM cached_tracks
            WHERE file_id IS NOT NULL AND audio_cache_key IS NOT NULL
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for row in rows:
            artists = self._artists_from_json(row["artists_json"])
            title = str(row["title"] or "")
            if not title or title.casefold() in {"yandex music", "\u044f\u043d\u0434\u0435\u043a\u0441 \u043c\u0443\u0437\u044b\u043a\u0430"}:
                continue
            track_key = canonical_track_key(title, artists, int(row["duration_ms"] or 0))
            await self._upsert_track_audio_cache(
                track_key=track_key,
                title=title,
                artists=artists,
                duration_ms=int(row["duration_ms"] or 0),
                source_url=str(row["spotify_url"] or ""),
                image_url=str(row["image_url"] or "") or None,
                cache_key=str(row["audio_cache_key"]),
                file_id=str(row["file_id"]),
                file_unique_id=str(row["file_unique_id"]) if row["file_unique_id"] else None,
                source=str(row["source"] or "legacy_cache"),
                local_path=Path(str(row["local_path"])) if row["local_path"] else None,
            )
            await self._save_track_alias(
                canonical_source_alias_key("track_id", str(row["track_id"])),
                track_key,
                "track_id",
                str(row["track_id"]),
                str(row["spotify_url"] or ""),
                title,
                ", ".join(artists),
                int(row["duration_ms"] or 0),
            )
            if row["spotify_url"]:
                await self._save_track_alias(
                    canonical_url_alias_key(str(row["spotify_url"])),
                    track_key,
                    "url",
                    "",
                    str(row["spotify_url"]),
                    title,
                    ", ".join(artists),
                    int(row["duration_ms"] or 0),
                )

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str,
        is_admin: bool,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, language_code, is_admin)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                language_code=COALESCE(users.language_code, excluded.language_code),
                is_admin=CASE WHEN users.is_admin = 1 OR excluded.is_admin = 1 THEN 1 ELSE 0 END,
                last_seen=CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name, last_name, language_code, int(is_admin)),
        )
        await self.db.commit()

    async def set_language(self, user_id: int, language_code: str) -> None:
        await self.db.execute(
            "UPDATE users SET language_code=?, last_seen=CURRENT_TIMESTAMP WHERE user_id=?",
            (language_code, user_id),
        )
        await self.db.commit()

    async def get_user_language(self, user_id: int) -> str | None:
        cursor = await self.db.execute("SELECT language_code FROM users WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["language_code"]) if row and row["language_code"] else None

    async def is_premium_user(self, user_id: int) -> bool:
        cursor = await self.db.execute(
            "SELECT is_premium, is_admin, is_friend, subscription_status FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return False
        return bool(
            row["is_premium"]
            or row["is_admin"]
            or row["is_friend"]
            or row["subscription_status"] in {"active", "premium"}
        )

    async def set_premium(self, user_id: int, enabled: bool) -> None:
        await self.db.execute(
            """
            INSERT INTO users (user_id, language_code, is_premium, subscription_status)
            VALUES (?, 'ru', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                is_premium=excluded.is_premium,
                subscription_status=excluded.subscription_status,
                last_seen=CURRENT_TIMESTAMP
            """,
            (user_id, int(enabled), "premium" if enabled else "free"),
        )
        await self.db.commit()

    async def sync_admins(self, user_ids: set[int]) -> None:
        for user_id in user_ids:
            await self.set_admin(user_id, True, source="env")
        await self.db.commit()

    async def set_admin(self, user_id: int, enabled: bool, source: str = "manual") -> None:
        if enabled:
            await self.db.execute(
                """
                INSERT INTO admin_users (user_id, source)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET source=excluded.source
                """,
                (user_id, source),
            )
            await self.db.execute(
                """
                INSERT INTO users (user_id, language_code, is_admin)
                VALUES (?, 'ru', 1)
                ON CONFLICT(user_id) DO UPDATE SET is_admin=1, last_seen=CURRENT_TIMESTAMP
                """,
                (user_id,),
            )
        else:
            await self.db.execute("DELETE FROM admin_users WHERE user_id=?", (user_id,))
            await self.db.execute("UPDATE users SET is_admin=0 WHERE user_id=?", (user_id,))
        await self.db.commit()

    async def is_admin_user(self, user_id: int) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM admin_users WHERE user_id=?
            UNION
            SELECT 1 FROM users WHERE user_id=? AND is_admin=1
            LIMIT 1
            """,
            (user_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def admin_user_ids(self) -> list[int]:
        cursor = await self.db.execute(
            """
            SELECT user_id FROM admin_users
            UNION
            SELECT user_id FROM users WHERE is_admin=1
            """
        )
        rows: list[Any] = await cursor.fetchall()
        await cursor.close()
        return [int(row["user_id"]) for row in rows]

    async def sync_friends(self, user_ids: set[int]) -> None:
        for user_id in user_ids:
            await self.set_friend(user_id, True, note="env")
        await self.db.commit()

    async def set_friend(self, user_id: int, enabled: bool, note: str | None = None) -> None:
        if enabled:
            await self.db.execute(
                """
                INSERT INTO friends (user_id, note)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET note=excluded.note
                """,
                (user_id, note),
            )
            await self.db.execute(
                """
                INSERT INTO users (user_id, language_code, is_friend)
                VALUES (?, 'ru', 1)
                ON CONFLICT(user_id) DO UPDATE SET is_friend=1, last_seen=CURRENT_TIMESTAMP
                """,
                (user_id,),
            )
        else:
            await self.db.execute("DELETE FROM friends WHERE user_id=?", (user_id,))
            await self.db.execute("UPDATE users SET is_friend=0 WHERE user_id=?", (user_id,))
        await self.db.commit()

    async def is_friend_user(self, user_id: int) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM friends WHERE user_id=?
            UNION
            SELECT 1 FROM users WHERE user_id=? AND is_friend=1
            LIMIT 1
            """,
            (user_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def is_ad_free_user(self, user_id: int) -> bool:
        return await self.is_admin_user(user_id) or await self.is_friend_user(user_id)

    def _track_key(self, track: Any) -> str:
        return canonical_track_key(
            str(getattr(track, "name", "")),
            tuple(getattr(track, "artists", ()) or ()),
            int(getattr(track, "duration_ms", 0) or 0),
        )

    def _track_artists(self, track: Any) -> tuple[str, ...]:
        return tuple(str(artist) for artist in (getattr(track, "artists", ()) or ()) if str(artist).strip())

    def _artists_from_json(self, value: Any) -> tuple[str, ...]:
        try:
            payload = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = []
        if not isinstance(payload, list):
            return ()
        return tuple(str(artist) for artist in payload if str(artist).strip())

    async def _save_track_alias(
        self,
        alias_key: str,
        track_key: str,
        source: str,
        source_id: str,
        source_url: str,
        title: str,
        artist: str,
        duration_ms: int,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO track_aliases (
                alias_key, track_key, source, source_id, source_url,
                title, artist, duration_ms, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(alias_key) DO UPDATE SET
                track_key=excluded.track_key,
                source=excluded.source,
                source_id=excluded.source_id,
                source_url=excluded.source_url,
                title=excluded.title,
                artist=excluded.artist,
                duration_ms=excluded.duration_ms,
                updated_at=CURRENT_TIMESTAMP,
                last_used_at=CURRENT_TIMESTAMP
            """,
            (alias_key, track_key, source, source_id, source_url, title, artist, duration_ms),
        )

    async def _upsert_track_audio_cache(
        self,
        *,
        track_key: str,
        title: str,
        artists: tuple[str, ...],
        duration_ms: int,
        source_url: str,
        image_url: str | None,
        cache_key: str | None,
        file_id: str | None,
        file_unique_id: str | None,
        source: str,
        local_path: Path | None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO track_audio_cache (
                track_key, title, artists_json, artist, duration_ms, source_url, image_url,
                audio_cache_key, file_id, file_unique_id, source, local_path, last_used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(track_key) DO UPDATE SET
                title=excluded.title,
                artists_json=excluded.artists_json,
                artist=excluded.artist,
                duration_ms=CASE
                    WHEN excluded.duration_ms > 0 THEN excluded.duration_ms
                    ELSE track_audio_cache.duration_ms
                END,
                source_url=COALESCE(NULLIF(excluded.source_url, ''), track_audio_cache.source_url),
                image_url=COALESCE(excluded.image_url, track_audio_cache.image_url),
                audio_cache_key=COALESCE(excluded.audio_cache_key, track_audio_cache.audio_cache_key),
                file_id=COALESCE(excluded.file_id, track_audio_cache.file_id),
                file_unique_id=COALESCE(excluded.file_unique_id, track_audio_cache.file_unique_id),
                source=COALESCE(NULLIF(excluded.source, ''), track_audio_cache.source),
                local_path=COALESCE(excluded.local_path, track_audio_cache.local_path),
                updated_at=CURRENT_TIMESTAMP,
                last_used_at=CURRENT_TIMESTAMP
            """,
            (
                track_key,
                title,
                json.dumps(list(artists), ensure_ascii=False),
                ", ".join(artists),
                duration_ms,
                source_url,
                image_url,
                cache_key,
                file_id,
                file_unique_id,
                source,
                str(local_path) if local_path else None,
            ),
        )

    async def get_cached_file(self, cache_key: str) -> str | None:
        cursor = await self.db.execute("SELECT file_id FROM file_cache WHERE cache_key=?", (cache_key,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["file_id"]) if row else None

    async def save_cached_file(
        self,
        cache_key: str,
        file_id: str,
        file_unique_id: str | None,
        media_type: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO file_cache (cache_key, file_id, file_unique_id, media_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                file_id=excluded.file_id,
                file_unique_id=excluded.file_unique_id,
                media_type=excluded.media_type
            """,
            (cache_key, file_id, file_unique_id, media_type),
        )
        await self.db.commit()

    async def save_track_metadata(self, track: Any) -> None:
        artists = self._track_artists(track)
        track_key = self._track_key(track)
        track_id = str(getattr(track, "id", ""))
        title = str(getattr(track, "name", ""))
        source_url = str(getattr(track, "spotify_url", "") or "")
        duration_ms = int(getattr(track, "duration_ms", 0) or 0)
        await self.db.execute(
            """
            INSERT INTO cached_tracks (
                track_id, title, artists_json, album, duration_ms, spotify_url, image_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title,
                artists_json=excluded.artists_json,
                album=excluded.album,
                duration_ms=excluded.duration_ms,
                spotify_url=excluded.spotify_url,
                image_url=excluded.image_url,
                updated_at=CURRENT_TIMESTAMP,
                last_used_at=CURRENT_TIMESTAMP
            """,
            (
                track_id,
                title,
                json.dumps(list(artists), ensure_ascii=False),
                getattr(track, "album", ""),
                duration_ms,
                source_url,
                getattr(track, "image_url", None),
            ),
        )
        if track_id:
            await self._save_track_alias(
                canonical_source_alias_key("track_id", track_id),
                track_key,
                "track_id",
                track_id,
                source_url,
                title,
                ", ".join(artists),
                duration_ms,
            )
        if source_url:
            await self._save_track_alias(
                canonical_url_alias_key(source_url),
                track_key,
                "url",
                "",
                source_url,
                title,
                ", ".join(artists),
                duration_ms,
            )
        await self.db.commit()

    async def save_album_cache(self, collection: Any) -> None:
        tracks = [
            {
                "id": track.id,
                "track_key": self._track_key(track),
                "title": track.name,
                "artists": list(track.artists),
                "album": track.album,
                "duration_ms": track.duration_ms,
                "spotify_url": track.spotify_url,
                "image_url": track.image_url,
            }
            for track in collection.tracks
        ]
        await self.db.execute(
            """
            INSERT INTO cached_albums (
                album_id, name, total_tracks, spotify_url, image_url, tracks_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id) DO UPDATE SET
                name=excluded.name,
                total_tracks=excluded.total_tracks,
                spotify_url=excluded.spotify_url,
                image_url=excluded.image_url,
                tracks_json=excluded.tracks_json,
                updated_at=CURRENT_TIMESTAMP,
                last_used_at=CURRENT_TIMESTAMP
            """,
            (
                collection.id,
                collection.name,
                collection.total,
                collection.spotify_url,
                collection.image_url,
                json.dumps(tracks, ensure_ascii=False),
            ),
        )
        await self.db.commit()

    async def mark_track_audio_cached(
        self,
        track: Any,
        cache_key: str,
        file_id: str | None,
        file_unique_id: str | None,
        source: str,
        local_path: Path | None,
    ) -> None:
        await self.save_track_metadata(track)
        artists = self._track_artists(track)
        track_key = self._track_key(track)
        track_id = str(getattr(track, "id", ""))
        title = str(getattr(track, "name", ""))
        source_url = str(getattr(track, "spotify_url", "") or "")
        duration_ms = int(getattr(track, "duration_ms", 0) or 0)
        await self.db.execute(
            """
            UPDATE cached_tracks
            SET audio_cache_key=?,
                file_id=?,
                file_unique_id=?,
                source=?,
                local_path=?,
                updated_at=CURRENT_TIMESTAMP,
                last_used_at=CURRENT_TIMESTAMP
            WHERE track_id=?
            """,
            (cache_key, file_id, file_unique_id, source, str(local_path) if local_path else None, track_id),
        )
        await self._upsert_track_audio_cache(
            track_key=track_key,
            title=title,
            artists=artists,
            duration_ms=duration_ms,
            source_url=source_url,
            image_url=getattr(track, "image_url", None),
            cache_key=cache_key,
            file_id=file_id,
            file_unique_id=file_unique_id,
            source=source,
            local_path=local_path,
        )
        await self.db.commit()

    async def get_track_audio_cache(self, track_id: str, track: Any | None = None) -> dict[str, str | None] | None:
        cursor = await self.db.execute(
            """
            SELECT audio_cache_key, file_id, file_unique_id, source, local_path
            FROM cached_tracks
            WHERE track_id=?
              AND audio_cache_key IS NOT NULL
              AND file_id IS NOT NULL
            """,
            (track_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            return {
                "cache_key": str(row["audio_cache_key"]),
                "file_id": str(row["file_id"]),
                "file_unique_id": str(row["file_unique_id"]) if row["file_unique_id"] else None,
                "source": str(row["source"]) if row["source"] else None,
                "local_path": str(row["local_path"]) if row["local_path"] else None,
            }

        if track is not None:
            row = await self._get_track_audio_cache_by_key(
                self._track_key(track),
                int(getattr(track, "duration_ms", 0) or 0),
            )
            if row:
                return row

        alias_key = canonical_source_alias_key("track_id", track_id)
        cursor = await self.db.execute("SELECT track_key FROM track_aliases WHERE alias_key=?", (alias_key,))
        alias = await cursor.fetchone()
        await cursor.close()
        if not alias:
            return None
        return await self._get_track_audio_cache_by_key(str(alias["track_key"]), int(getattr(track, "duration_ms", 0) or 0))

    async def _get_track_audio_cache_by_key(
        self,
        track_key: str,
        duration_ms: int = 0,
    ) -> dict[str, str | None] | None:
        cursor = await self.db.execute(
            """
            SELECT audio_cache_key, file_id, file_unique_id, source, local_path, duration_ms
            FROM track_audio_cache
            WHERE track_key=?
              AND audio_cache_key IS NOT NULL
              AND file_id IS NOT NULL
            """,
            (track_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        cached_duration = int(row["duration_ms"] or 0)
        if duration_ms > 0 and cached_duration > 0 and abs(duration_ms - cached_duration) > 6000:
            return None
        await self.db.execute(
            "UPDATE track_audio_cache SET last_used_at=CURRENT_TIMESTAMP WHERE track_key=?",
            (track_key,),
        )
        await self.db.commit()
        return {
            "cache_key": str(row["audio_cache_key"]),
            "file_id": str(row["file_id"]),
            "file_unique_id": str(row["file_unique_id"]) if row["file_unique_id"] else None,
            "source": str(row["source"]) if row["source"] else None,
            "local_path": str(row["local_path"]) if row["local_path"] else None,
        }

    async def get_cached_track_by_url(self, url: str) -> dict[str, Any] | None:
        alias_key = canonical_url_alias_key(url)
        cursor = await self.db.execute(
            """
            SELECT c.track_key, c.title, c.artists_json, c.duration_ms, c.source_url,
                   c.image_url, c.audio_cache_key, c.file_id, c.file_unique_id, c.source,
                   c.local_path
            FROM track_aliases a
            JOIN track_audio_cache c ON c.track_key=a.track_key
            WHERE a.alias_key=?
              AND c.file_id IS NOT NULL
              AND c.audio_cache_key IS NOT NULL
            LIMIT 1
            """,
            (alias_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        await self.db.execute(
            "UPDATE track_aliases SET last_used_at=CURRENT_TIMESTAMP WHERE alias_key=?",
            (alias_key,),
        )
        await self.db.execute(
            "UPDATE track_audio_cache SET last_used_at=CURRENT_TIMESTAMP WHERE track_key=?",
            (row["track_key"],),
        )
        await self.db.commit()
        return dict(row)

    async def save_source_candidates(self, track_id: str, candidates: list[Any]) -> None:
        for candidate in candidates:
            await self.db.execute(
                """
                INSERT INTO source_candidates (
                    track_id, source, source_id, title, artist, url,
                    thumbnail_url, score, is_exact, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(track_id, source, source_id) DO UPDATE SET
                    title=excluded.title,
                    artist=excluded.artist,
                    url=excluded.url,
                    thumbnail_url=excluded.thumbnail_url,
                    score=excluded.score,
                    is_exact=excluded.is_exact,
                    last_used_at=CURRENT_TIMESTAMP
                """,
                (
                    track_id,
                    str(getattr(candidate, "source", "youtube_music")),
                    str(getattr(candidate, "source_id", "")),
                    str(getattr(candidate, "title", "")),
                    str(getattr(candidate, "artist", "")),
                    str(getattr(candidate, "url", "")),
                    getattr(candidate, "thumbnail_url", None),
                    int(getattr(candidate, "score", 0)),
                    int(bool(getattr(candidate, "is_exact", False))),
                ),
            )
        await self.db.commit()

    async def get_source_candidate(
        self,
        track_id: str,
        source_id: str,
        source: str = "youtube_music",
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            """
            SELECT *
            FROM source_candidates
            WHERE track_id=? AND source=? AND source_id=?
            LIMIT 1
            """,
            (track_id, source, source_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None
        await self.db.execute(
            "UPDATE source_candidates SET last_used_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
        await self.db.commit()
        return dict(row)

    async def record_download(self, user_id: int, object_id: str, source: str) -> int:
        safe_object_id = str(object_id or "unknown")
        await self.db.execute(
            "INSERT INTO downloads (user_id, object_id, source) VALUES (?, ?, ?)",
            (user_id, safe_object_id, source),
        )
        await self.db.execute(
            """
            INSERT INTO user_counters (user_id, downloads_count)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                downloads_count=user_counters.downloads_count + 1
            """,
            (user_id,),
        )
        await self.db.execute(
            """
            INSERT INTO users (user_id, language_code, downloads_count, last_download_at)
            VALUES (?, 'ru', 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                downloads_count=users.downloads_count + 1,
                last_download_at=CURRENT_TIMESTAMP,
                last_seen=CURRENT_TIMESTAMP
            """,
            (user_id,),
        )
        await self.db.commit()

        cursor = await self.db.execute(
            "SELECT downloads_count FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["downloads_count"]) if row else 0

    async def add_ad(
        self,
        placement: str,
        text: str,
        name: str | None = None,
        *,
        media_type: str | None = None,
        media_file_id: str | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        buttons: list[dict[str, str]] | None = None,
    ) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO ads (
                name, placement, text, media_type, media_file_id,
                source_chat_id, source_message_id, buttons_json, status, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1)
            """,
            (
                name or placement,
                placement,
                text,
                media_type,
                media_file_id,
                source_chat_id,
                source_message_id,
                json.dumps(buttons or [], ensure_ascii=False),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def list_ads(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT id, name, placement, text, media_type, buttons_json, status, is_active, is_deleted, created_at
            FROM ads
            WHERE is_deleted=0
            ORDER BY id DESC
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def set_ad_active(self, ad_id: int, enabled: bool) -> None:
        await self.db.execute(
            """
            UPDATE ads
            SET is_active=?, status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(enabled), "ACTIVE" if enabled else "PAUSED", ad_id),
        )
        await self.db.commit()

    async def delete_ad(self, ad_id: int) -> None:
        await self.db.execute(
            """
            UPDATE ads
            SET is_active=0, is_deleted=1, status='DELETED', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (ad_id,),
        )
        await self.db.commit()

    async def get_ad(self, ad_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute("SELECT * FROM ads WHERE id=?", (ad_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def active_ads_for_user(self, user_id: int, placement: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT *
            FROM ads
            WHERE placement=? AND is_active=1 AND is_deleted=0
            ORDER BY id
            """,
            (placement,),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        ads = [dict(row) for row in rows]
        if placement != "every_8h":
            return ads

        due: list[dict[str, Any]] = []
        for ad in ads:
            cursor = await self.db.execute(
                """
                SELECT sent_at
                FROM ad_delivery_log
                WHERE user_id=? AND ad_id=?
                ORDER BY sent_at DESC
                LIMIT 1
                """,
                (user_id, ad["id"]),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                due.append(ad)
                continue

            cursor = await self.db.execute(
                "SELECT datetime(?) <= datetime('now', '-8 hours') AS is_due",
                (row["sent_at"],),
            )
            due_row = await cursor.fetchone()
            await cursor.close()
            if due_row and int(due_row["is_due"]):
                due.append(ad)
        return due

    async def record_ad_sent(self, user_id: int, ad_id: int, placement: str) -> None:
        await self.db.execute(
            "INSERT INTO ad_delivery_log (user_id, ad_id, placement) VALUES (?, ?, ?)",
            (user_id, ad_id, placement),
        )
        await self.db.commit()

    async def sync_required_channels(self, channels: list[str]) -> None:
        for channel in channels:
            await self.add_required_link("telegram_channel", channel, channel)
        await self.db.commit()

    async def add_required_link(self, kind: str, value: str, title: str | None = None) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO required_links (kind, value, title)
            VALUES (?, ?, ?)
            ON CONFLICT(value) DO UPDATE SET
                kind=excluded.kind,
                title=excluded.title,
                is_active=1,
                updated_at=CURRENT_TIMESTAMP
            """,
            (kind, value, title),
        )
        await self.db.commit()
        return int(cursor.lastrowid or 0)

    async def set_required_active(self, link_id: int, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE required_links SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(enabled), link_id),
        )
        await self.db.commit()

    async def active_required_links(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT id, kind, value, title FROM required_links WHERE is_active=1 ORDER BY id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def get_setting(self, key: str) -> str | None:
        cursor = await self.db.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cursor.fetchone()
        await cursor.close()
        return str(row["value"]) if row and row["value"] is not None else None

    async def set_setting(self, key: str, value: str | None) -> None:
        await self.db.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        await self.db.commit()

    async def is_maintenance_on(self) -> bool:
        return (await self.get_setting("maintenance_on")) == "1"

    async def set_maintenance(self, enabled: bool, text: str | None = None) -> None:
        await self.set_setting("maintenance_on", "1" if enabled else "0")
        if text is not None:
            await self.set_setting("maintenance_text", text)

    async def ban_user(self, user_id: int, reason: str | None = None) -> None:
        await self.db.execute(
            """
            INSERT INTO banned_users (user_id, reason)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason
            """,
            (user_id, reason),
        )
        await self.db.commit()

    async def unban_user(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
        await self.db.commit()

    async def is_banned(self, user_id: int) -> bool:
        cursor = await self.db.execute("SELECT 1 FROM banned_users WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def add_report(self, user_id: int, text: str) -> int:
        cursor = await self.db.execute(
            "INSERT INTO reports (user_id, text) VALUES (?, ?)",
            (user_id, text),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def record_failed_download(self, user_id: int | None, object_id: str | None, reason: str) -> None:
        await self.db.execute(
            "INSERT INTO failed_downloads (user_id, object_id, reason) VALUES (?, ?, ?)",
            (user_id, object_id, reason),
        )
        await self.db.commit()

    async def log_error(self, scope: str, message: str, traceback_text: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO bot_errors (scope, message, traceback) VALUES (?, ?, ?)",
            (scope, message, traceback_text),
        )
        await self.db.commit()

    async def stats(self) -> dict[str, int]:
        result: dict[str, int] = {}
        queries: dict[str, str] = {
            "users": "SELECT COUNT(*) AS value FROM users",
            "downloads": "SELECT COUNT(*) AS value FROM downloads",
            "cache": "SELECT COUNT(*) AS value FROM file_cache",
            "cached_tracks": "SELECT COUNT(*) AS value FROM cached_tracks",
            "track_audio_cache": "SELECT COUNT(*) AS value FROM track_audio_cache WHERE file_id IS NOT NULL",
            "cached_albums": "SELECT COUNT(*) AS value FROM cached_albums",
            "source_candidates": "SELECT COUNT(*) AS value FROM source_candidates",
            "ads": "SELECT COUNT(*) AS value FROM ads",
            "required_links": "SELECT COUNT(*) AS value FROM required_links WHERE is_active=1",
            "friends": "SELECT COUNT(*) AS value FROM friends",
            "admins": "SELECT COUNT(*) AS value FROM admin_users",
        }
        for key, query in queries.items():
            cursor = await self.db.execute(query)
            row = await cursor.fetchone()
            await cursor.close()
            result[key] = int(row["value"]) if row else 0
        return result

    async def all_user_ids(self) -> list[int]:
        cursor = await self.db.execute("SELECT user_id FROM users")
        rows: list[Any] = await cursor.fetchall()
        await cursor.close()
        return [int(row["user_id"]) for row in rows]
