from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .config import Config
from .spotify import SpotifyCollection, SpotifyTrack

try:
    from ytmusicapi import YTMusic
except ImportError:  # pragma: no cover - optional dependency on old installs
    YTMusic = None  # type: ignore[assignment]


YOUTUBE_MUSIC_PLAYLIST_URL = "https://music.youtube.com/playlist"
YOUTUBE_MUSIC_WATCH_URL = "https://music.youtube.com/watch"


class YouTubeMusicPlaylistError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeMusicPlaylistTrack:
    video_id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int
    image_url: str | None

    @property
    def url(self) -> str:
        return f"{YOUTUBE_MUSIC_WATCH_URL}?v={self.video_id}"

    @property
    def artist_names(self) -> str:
        return ", ".join(self.artists) if self.artists else "YouTube Music"

    def to_spotify_track(self, playlist_name: str) -> SpotifyTrack:
        return SpotifyTrack(
            id=f"ytm_{self.video_id}",
            name=self.title or "YouTube Music",
            artists=self.artists or ("YouTube Music",),
            album=playlist_name or "YouTube Music",
            duration_ms=self.duration_ms,
            spotify_url=self.url,
            image_url=self.image_url,
        )

    def to_source_candidate(self) -> dict[str, object]:
        return {
            "source": "youtube_music",
            "source_id": self.video_id,
            "title": self.title,
            "artist": self.artist_names,
            "url": self.url,
            "thumbnail_url": self.image_url,
            "score": 100,
            "is_exact": True,
        }


@dataclass(frozen=True, slots=True)
class YouTubeMusicPlaylist:
    playlist_id: str
    name: str
    total: int
    tracks: tuple[YouTubeMusicPlaylistTrack, ...]
    url: str
    image_url: str | None

    def to_collection(self) -> SpotifyCollection:
        return SpotifyCollection(
            kind="playlist",
            id=self.playlist_id,
            name=self.name,
            total=self.total,
            tracks=tuple(track.to_spotify_track(self.name) for track in self.tracks),
            spotify_url=self.url,
            image_url=self.image_url,
        )


class YouTubeMusicPlaylistResolver:
    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return YTMusic is not None

    async def get_playlist(self, playlist_id: str, limit: int) -> YouTubeMusicPlaylist:
        playlist_id = str(playlist_id or "").strip()
        if not playlist_id:
            raise YouTubeMusicPlaylistError("playlist id is empty")
        if YTMusic is None:
            raise YouTubeMusicPlaylistError("ytmusicapi is not installed")
        return await asyncio.to_thread(self._get_playlist_sync, playlist_id, max(1, int(limit or 1)))

    def _get_playlist_sync(self, playlist_id: str, limit: int) -> YouTubeMusicPlaylist:
        try:
            payload = YTMusic().get_playlist(playlist_id, limit=limit)
        except Exception as error:  # pragma: no cover - external service
            raise YouTubeMusicPlaylistError(str(error)) from error

        if not isinstance(payload, dict):
            raise YouTubeMusicPlaylistError("playlist metadata is empty")

        name = str(payload.get("title") or "YouTube Music playlist").strip()
        tracks_payload = payload.get("tracks")
        if not isinstance(tracks_payload, list):
            tracks_payload = []

        tracks: list[YouTubeMusicPlaylistTrack] = []
        for item in tracks_payload[:limit]:
            if not isinstance(item, dict):
                continue
            video_id = str(item.get("videoId") or "").strip()
            if not video_id:
                continue
            title = str(item.get("title") or "YouTube Music").strip()
            artists = _artists_from_payload(item.get("artists"))
            tracks.append(
                YouTubeMusicPlaylistTrack(
                    video_id=video_id,
                    title=title,
                    artists=artists,
                    duration_ms=_duration_to_ms(item.get("duration_seconds") or item.get("duration")),
                    image_url=_best_thumbnail(item.get("thumbnails")),
                )
            )

        total = int(payload.get("trackCount") or payload.get("count") or len(tracks_payload) or len(tracks))
        image_url = _best_thumbnail(payload.get("thumbnails"))
        if image_url is None and tracks:
            image_url = tracks[0].image_url

        return YouTubeMusicPlaylist(
            playlist_id=playlist_id,
            name=name,
            total=total,
            tracks=tuple(tracks),
            url=f"{YOUTUBE_MUSIC_PLAYLIST_URL}?list={playlist_id}",
            image_url=image_url,
        )


def _artists_from_payload(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ("YouTube Music",)
    names = []
    for item in value:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return tuple(names) or ("YouTube Music",)


def _duration_to_ms(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int | float):
        seconds = int(value)
        return max(0, seconds * 1000)

    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text) * 1000
    parts = text.split(":")
    try:
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return max(0, seconds * 1000)
    except ValueError:
        return 0


def _best_thumbnail(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, list):
        return None
    best_url = None
    best_width = -1
    for item in thumbnails:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        try:
            width = int(item.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        if width >= best_width:
            best_url = url
            best_width = width
    return best_url
