from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import Config
from .spotify import SpotifyTrack


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_MUSIC_WATCH_URL = "https://music.youtube.com/watch"

NOISE_TOKENS = {
    "official",
    "video",
    "audio",
    "lyrics",
    "lyric",
    "visualizer",
    "clip",
    "hd",
    "hq",
    "mv",
    "music",
}

VERSION_TOKENS = {
    "cover",
    "karaoke",
    "instrumental",
    "live",
    "remix",
    "sped",
    "speed",
    "slowed",
    "reverb",
    "nightcore",
    "edit",
}


class YouTubeMusicMatcherError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeMusicCandidate:
    source: str
    source_id: str
    title: str
    artist: str
    url: str
    thumbnail_url: str | None
    score: int
    is_exact: bool

    def to_provider_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "artist": self.artist,
            "url": self.url,
            "thumbnail_url": self.thumbnail_url,
            "score": self.score,
            "is_exact": self.is_exact,
        }


class YouTubeMusicMatcher:
    def __init__(self, config: Config, session: aiohttp.ClientSession) -> None:
        self.config = config
        self.session = session

    @property
    def enabled(self) -> bool:
        return bool(self.config.youtube_music_search_enabled and self.config.youtube_api_key)

    async def search(self, track: SpotifyTrack, limit: int = 3) -> list[YouTubeMusicCandidate]:
        if not self.enabled:
            return []

        params = {
            "key": self.config.youtube_api_key,
            "part": "snippet",
            "type": "video",
            "videoCategoryId": "10",
            "maxResults": str(max(1, min(limit, 10))),
            "q": f"{track.artist_names} {track.name}",
            "regionCode": self.config.youtube_region_code,
            "safeSearch": "none",
        }

        async with self.session.get(YOUTUBE_SEARCH_URL, params=params) as response:
            if response.status >= 400:
                body = await response.text()
                raise YouTubeMusicMatcherError(f"YouTube Data API returned {response.status}: {body[:240]}")
            payload = await response.json(content_type=None)

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        candidates: list[YouTubeMusicCandidate] = []
        for item in items:
            candidate = self._candidate_from_item(track, item)
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= limit:
                break
        return candidates

    async def get_track_candidate(self, video_id: str) -> YouTubeMusicCandidate:
        video_id = str(video_id or "").strip()
        if not video_id:
            raise YouTubeMusicMatcherError("YouTube Music video id is empty")

        if not self.enabled:
            return YouTubeMusicCandidate(
                source="youtube_music",
                source_id=video_id,
                title="YouTube Music",
                artist="YouTube Music",
                url=f"{YOUTUBE_MUSIC_WATCH_URL}?v={video_id}",
                thumbnail_url=None,
                score=100,
                is_exact=True,
            )

        params = {
            "key": self.config.youtube_api_key,
            "part": "snippet,contentDetails",
            "id": video_id,
        }
        async with self.session.get(YOUTUBE_VIDEOS_URL, params=params) as response:
            if response.status >= 400:
                body = await response.text()
                raise YouTubeMusicMatcherError(f"YouTube Data API returned {response.status}: {body[:240]}")
            payload = await response.json(content_type=None)

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise YouTubeMusicMatcherError("YouTube Music video was not found")

        snippet = items[0].get("snippet") or {}
        if not isinstance(snippet, dict):
            raise YouTubeMusicMatcherError("YouTube Music video metadata is empty")

        title = html.unescape(str(snippet.get("title") or "")).strip()
        artist = html.unescape(str(snippet.get("channelTitle") or "")).strip()
        return YouTubeMusicCandidate(
            source="youtube_music",
            source_id=video_id,
            title=title or "YouTube Music",
            artist=artist or "YouTube Music",
            url=f"{YOUTUBE_MUSIC_WATCH_URL}?v={video_id}",
            thumbnail_url=_best_thumbnail(snippet.get("thumbnails")),
            score=100,
            is_exact=True,
        )

    def _candidate_from_item(self, track: SpotifyTrack, item: dict[str, Any]) -> YouTubeMusicCandidate | None:
        video_id = str((item.get("id") or {}).get("videoId") or "").strip()
        snippet = item.get("snippet") or {}
        if not video_id or not isinstance(snippet, dict):
            return None

        title = html.unescape(str(snippet.get("title") or "")).strip()
        artist = html.unescape(str(snippet.get("channelTitle") or "")).strip()
        thumbnail_url = _best_thumbnail(snippet.get("thumbnails"))
        score, is_exact = score_candidate(track, title, artist)

        return YouTubeMusicCandidate(
            source="youtube_music",
            source_id=video_id,
            title=title or "YouTube Music",
            artist=artist or "YouTube Music",
            url=f"{YOUTUBE_MUSIC_WATCH_URL}?v={video_id}",
            thumbnail_url=thumbnail_url,
            score=score,
            is_exact=is_exact,
        )


def score_candidate(track: SpotifyTrack, candidate_title: str, candidate_artist: str) -> tuple[int, bool]:
    title_tokens = _tokenize(track.name, keep_version_tokens=True)
    if not title_tokens:
        return 0, False

    candidate_tokens = _tokenize(
        f"{candidate_title} {candidate_artist}",
        keep_version_tokens=True,
    )
    candidate_title_tokens = _tokenize(candidate_title, keep_version_tokens=True)
    if not candidate_tokens:
        return 0, False

    title_overlap = len(title_tokens & candidate_title_tokens)
    title_coverage = title_overlap / max(1, len(title_tokens))

    artist_match = any(_artist_matches(artist, candidate_tokens) for artist in track.artists)
    score = int(title_coverage * 72)
    if artist_match:
        score += 24
    if _normalized(track.name) in _normalized(candidate_title):
        score += 8

    requested_version_tokens = title_tokens & VERSION_TOKENS
    candidate_version_tokens = candidate_title_tokens & VERSION_TOKENS
    unexpected_version = bool(candidate_version_tokens - requested_version_tokens)
    if unexpected_version:
        score -= 18

    score = max(0, min(100, score))
    is_exact = title_coverage >= 0.92 and artist_match and not unexpected_version
    return score, is_exact


def _artist_matches(artist: str, candidate_tokens: set[str]) -> bool:
    artist_tokens = _tokenize(artist, keep_version_tokens=False)
    if not artist_tokens:
        return False
    if len(artist_tokens) == 1:
        return next(iter(artist_tokens)) in candidate_tokens
    return len(artist_tokens & candidate_tokens) / len(artist_tokens) >= 0.75


def _tokenize(value: str, *, keep_version_tokens: bool) -> set[str]:
    normalized = _normalized(value)
    tokens = {token for token in normalized.split() if len(token) > 1}
    tokens -= NOISE_TOKENS
    if not keep_version_tokens:
        tokens -= VERSION_TOKENS
    return tokens


def _normalized(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^\w\s]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _best_thumbnail(thumbnails: Any) -> str | None:
    if not isinstance(thumbnails, dict):
        return None
    for key in ("maxres", "standard", "high", "medium", "default"):
        item = thumbnails.get(key)
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return None
