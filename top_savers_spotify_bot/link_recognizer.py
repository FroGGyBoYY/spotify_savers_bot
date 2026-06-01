from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

from .parser import SpotifyLink, parse_spotify_link


URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
YOUTUBE_VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
VK_MUSIC_HOSTS = {
    "vk.com",
    "www.vk.com",
    "m.vk.com",
    "vk.ru",
    "www.vk.ru",
    "m.vk.ru",
    "music.vk.com",
    "music.vk.ru",
}


class LinkKind(str, Enum):
    SPOTIFY = "spotify"
    YOUTUBE_MUSIC_TRACK = "youtube_music_track"
    YOUTUBE_MUSIC_PLAYLIST = "youtube_music_playlist"
    YOUTUBE_VIDEO = "youtube_video"
    EXTERNAL_MUSIC = "external_music"
    UNKNOWN_URL = "unknown_url"
    NO_URL = "no_url"


@dataclass(frozen=True, slots=True)
class RecognizedLink:
    kind: LinkKind
    url: str | None = None
    spotify: SpotifyLink | None = None
    video_id: str | None = None
    playlist_id: str | None = None


EXTERNAL_MUSIC_HOSTS = {
    "music.apple.com",
    "itunes.apple.com",
    "spotify.link",
    "spotify.app.link",
    "music.yandex.ru",
    "music.yandex.com",
    "soundcloud.com",
    "www.soundcloud.com",
    "on.soundcloud.com",
    "deezer.com",
    "www.deezer.com",
    "tidal.com",
    "www.tidal.com",
    "listen.tidal.com",
    "bandcamp.com",
    "www.bandcamp.com",
    "zvuk.com",
    "sber-zvuk.com",
    "music.amazon.com",
    "music.amazon.co.uk",
    "music.amazon.de",
    "music.amazon.es",
    "music.amazon.fr",
    "music.amazon.it",
    "music.amazon.co.jp",
    "qobuz.com",
    "www.qobuz.com",
    "audiomack.com",
    "www.audiomack.com",
    "anghami.com",
    "play.anghami.com",
    "shazam.com",
    "www.shazam.com",
    "genius.com",
    "www.genius.com",
    "last.fm",
    "www.last.fm",
    "pandora.com",
    "www.pandora.com",
    "music.vk.com",
    "music.vk.ru",
    "boom.ru",
    "music.boom.ru",
    "napster.com",
    "music.napster.com",
    "iheart.com",
    "www.iheart.com",
    "jiosaavn.com",
    "www.jiosaavn.com",
    "gaana.com",
    "www.gaana.com",
    "wynk.in",
    "music.youtube.com",
    "song.link",
    "odesli.co",
    "lnk.to",
    "ffm.to",
    "found.ee",
    "linkfire.com",
}


def recognize_link(text: str) -> RecognizedLink:
    spotify = parse_spotify_link(text)
    if spotify is not None:
        return RecognizedLink(kind=LinkKind.SPOTIFY, spotify=spotify)

    match = URL_RE.search(text or "")
    if not match:
        return RecognizedLink(kind=LinkKind.NO_URL)

    url = match.group(0).rstrip(").,]")
    parsed = urlparse(url)
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]

    if host == "music.youtube.com":
        video_id = _youtube_video_id(parsed)
        if video_id:
            return RecognizedLink(
                kind=LinkKind.YOUTUBE_MUSIC_TRACK,
                url=f"https://music.youtube.com/watch?v={video_id}",
                video_id=video_id,
            )
        playlist_id = _youtube_playlist_id(parsed)
        if playlist_id:
            return RecognizedLink(
                kind=LinkKind.YOUTUBE_MUSIC_PLAYLIST,
                url=f"https://music.youtube.com/playlist?list={playlist_id}",
                playlist_id=playlist_id,
            )
        return RecognizedLink(kind=LinkKind.UNKNOWN_URL, url=url)

    if host in YOUTUBE_VIDEO_HOSTS or host.endswith(".youtube.com"):
        return RecognizedLink(kind=LinkKind.YOUTUBE_VIDEO, url=url, video_id=_youtube_video_id(parsed))

    if _is_external_music_host(host, parsed.path):
        return RecognizedLink(kind=LinkKind.EXTERNAL_MUSIC, url=url)

    return RecognizedLink(kind=LinkKind.UNKNOWN_URL, url=url)


def _youtube_video_id(parsed) -> str | None:
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    path = parsed.path.strip("/")

    if host == "youtu.be" and path:
        return path.split("/", 1)[0]

    query_video_id = (parse_qs(parsed.query).get("v") or [""])[0].strip()
    if query_video_id:
        return query_video_id

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1]
    return None


def _youtube_playlist_id(parsed) -> str | None:
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host != "music.youtube.com":
        return None

    path = parsed.path.strip("/").lower()
    playlist_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    if playlist_id and (path == "playlist" or not _youtube_video_id(parsed)):
        return playlist_id
    return None


def _is_external_music_host(host: str, path: str) -> bool:
    if host in EXTERNAL_MUSIC_HOSTS or any(host.endswith(f".{item}") for item in EXTERNAL_MUSIC_HOSTS):
        return True

    normalized_path = path.strip("/").lower()
    if host in VK_MUSIC_HOSTS:
        return normalized_path.startswith(("audio", "music", "artist", "audios"))

    if host in {"yandex.ru", "www.yandex.ru", "yandex.com", "www.yandex.com"}:
        return normalized_path.startswith(("music/", "album/", "track/"))

    if host.endswith(".bandcamp.com"):
        return True

    if "music" in host and not host.endswith(".youtube.com"):
        return True

    return False
