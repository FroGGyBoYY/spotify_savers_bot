from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from .vk_music import VK_MUSIC_HOSTS, VkMusicMetadataResolver


class ExternalMusicResolverError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalMusicTrack:
    source: str
    source_name: str
    source_id: str
    title: str
    artist: str
    url: str
    image_url: str | None = None

    @property
    def search_query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    def to_source_candidate(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
            "artist": self.artist,
            "thumbnail_url": self.image_url,
            "score": 0,
            "is_exact": False,
            "search_query": self.search_query,
            "source_url": self.url,
        }


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag == "script":
            values = {name.lower(): value or "" for name, value in attrs}
            script_type = values.get("type", "").split(";", 1)[0].strip().lower()
            if script_type == "application/ld+json":
                self._in_json_ld = True
            return
        if tag != "meta":
            return

        values = {name.lower(): value or "" for name, value in attrs}
        key = values.get("property") or values.get("name") or values.get("itemprop")
        content = values.get("content")
        if key and content:
            self.meta[key.lower()] = html.unescape(content).strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() == "script":
            self._in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_json_ld:
            self.json_ld_parts.append(data)

    @property
    def title(self) -> str:
        return clean_text(" ".join(self.title_parts))


class ExternalMusicResolver:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        vk_access_token: str = "",
        vk_browser_metadata_enabled: bool = False,
        vk_browser_metadata_timeout_seconds: int = 18,
    ) -> None:
        self.session = session
        self.vk_music = VkMusicMetadataResolver(
            session,
            vk_access_token,
            browser_metadata_enabled=vk_browser_metadata_enabled,
            browser_timeout_seconds=vk_browser_metadata_timeout_seconds,
        )

    async def resolve(self, url: str) -> ExternalMusicTrack:
        url = str(url or "").strip()
        if not url:
            raise ExternalMusicResolverError("empty url")

        try:
            provider_track = await self._resolve_provider(url)
        except (aiohttp.ClientError, OSError, ValueError):
            provider_track = None
        if provider_track is not None:
            return provider_track

        html_text = await self._fetch(url)
        parser = _MetaParser()
        parser.feed(html_text[:1_500_000])

        structured = parse_json_ld_music(parser.json_ld_parts)
        title, artist = parse_title_artist(
            url,
            parser.meta,
            parser.title,
            structured_title=structured.get("title", ""),
            structured_artist=structured.get("artist", ""),
        )
        if not title:
            raise ExternalMusicResolverError("could not detect track title")
        if is_generic_metadata(title, artist):
            raise ExternalMusicResolverError("metadata page returned generic music-service title")

        source, source_name = source_from_url(url)
        return ExternalMusicTrack(
            source=source,
            source_name=source_name,
            source_id=hashlib.sha1(url.encode("utf-8")).hexdigest()[:16],
            title=title,
            artist=artist or "Unknown artist",
            url=url,
            image_url=structured.get("image") or best_image(parser.meta),
        )

    async def _resolve_provider(self, url: str) -> ExternalMusicTrack | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower().split("@")[-1].split(":")[0]
        if "music.yandex." in host:
            return await self._resolve_yandex_music(url, parsed)
        if host in VK_MUSIC_HOSTS:
            return await self._resolve_vk_music(url, parsed)
        if "music.apple.com" in host or "itunes.apple.com" in host:
            return await self._resolve_apple_music(url, parsed)
        if "soundcloud.com" in host:
            return await self._resolve_oembed(
                url,
                "https://soundcloud.com/oembed?format=json&url={url}",
                "soundcloud",
                "SoundCloud",
            )
        if "deezer.com" in host:
            return await self._resolve_oembed(
                url,
                "https://api.deezer.com/oembed?url={url}",
                "deezer",
                "Deezer",
            )
        return None

    async def _resolve_vk_music(self, url: str, parsed) -> ExternalMusicTrack | None:
        vk_track = await self.vk_music.resolve(parsed)
        if vk_track is None or is_generic_metadata(vk_track.title, vk_track.artist):
            return None

        return ExternalMusicTrack(
            source="vk_music",
            source_name="VK Music",
            source_id=vk_track.source_id,
            title=vk_track.title,
            artist=vk_track.artist,
            url=url,
            image_url=vk_track.image_url,
        )

    async def _resolve_yandex_music(self, url: str, parsed) -> ExternalMusicTrack | None:
        track_id, album_id = yandex_ids_from_path(parsed.path)
        api_track = await self._resolve_yandex_api(url, track_id, album_id)
        if api_track is not None:
            return api_track

        html_text = await self._fetch_yandex_crawler(url)
        parser = _MetaParser()
        parser.feed(html_text[:2_500_000])

        title = first_meta(parser.meta, "og:title", "twitter:title", "music:song:title")
        artist = yandex_artist_from_description(
            first_meta(parser.meta, "og:description", "twitter:description", "description")
        )
        if not artist:
            artist = yandex_artist_from_state(html_text, track_id)
        if not title:
            title, parsed_artist = parse_yandex_page_title(parser.title)
            artist = artist or parsed_artist

        title = strip_service_noise(title)
        artist = strip_service_noise(artist)
        if not title or is_generic_metadata(title, artist):
            return None

        source_id = "_".join(filter(None, ("yandex", track_id, album_id)))
        if not source_id:
            source_id = f"yandex_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}"
        return ExternalMusicTrack(
            source="yandex_music",
            source_name="Yandex Music",
            source_id=source_id,
            title=title,
            artist=artist or "Unknown artist",
            url=url,
            image_url=best_image(parser.meta) or yandex_cover_from_state(html_text, track_id),
        )

    async def _resolve_yandex_api(
        self,
        url: str,
        track_id: str,
        album_id: str,
    ) -> ExternalMusicTrack | None:
        if not track_id:
            return None
        api_id = f"{track_id}:{album_id}" if album_id else track_id
        endpoint = f"https://api.music.yandex.net/tracks/{api_id}"
        try:
            async with self.session.get(
                endpoint,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "User-Agent": "TelegramBot (like TwitterBot)",
                },
            ) as response:
                if response.status >= 400:
                    return None
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, OSError, ValueError, json.JSONDecodeError):
            return None

        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, list) and result:
            payload = result[0]
        elif isinstance(result, dict):
            payload = result
        else:
            return None
        if not isinstance(payload, dict):
            return None

        title = clean_text(payload.get("title"))
        artist = ", ".join(
            filter(None, (clean_text(item.get("name")) for item in payload.get("artists", []) if isinstance(item, dict)))
        )
        image_url = yandex_cover_url(clean_text(payload.get("coverUri")))
        if not image_url:
            albums = payload.get("albums")
            if isinstance(albums, list) and albums and isinstance(albums[0], dict):
                image_url = yandex_cover_url(clean_text(albums[0].get("coverUri")))
        if not title or is_generic_metadata(title, artist):
            return None
        return ExternalMusicTrack(
            source="yandex_music",
            source_name="Yandex Music",
            source_id="_".join(filter(None, ("yandex", track_id, album_id))),
            title=title,
            artist=artist or "Unknown artist",
            url=url,
            image_url=image_url,
        )

    async def _resolve_apple_music(self, url: str, parsed) -> ExternalMusicTrack | None:
        parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        apple_id = (query.get("i") or [""])[0]
        if not apple_id and parts:
            apple_id = next((part for part in reversed(parts) if part.isdigit()), "")
        if not apple_id:
            return None

        country = parts[0].lower() if parts and re.fullmatch(r"[a-z]{2}", parts[0].lower()) else "us"
        lookup_url = f"https://itunes.apple.com/lookup?id={apple_id}&country={country}"
        async with self.session.get(lookup_url, headers={"Accept": "application/json"}) as response:
            if response.status >= 400:
                return None
            data = await response.json(content_type=None)

        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            return None
        item = next(
            (
                result
                for result in results
                if isinstance(result, dict)
                and str(result.get("wrapperType") or "").lower() == "track"
                and str(result.get("kind") or "").lower() == "song"
            ),
            results[0],
        )
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("trackName") or item.get("collectionName"))
        artist = clean_text(item.get("artistName"))
        if not title or is_generic_metadata(title, artist):
            return None
        source_id = str(item.get("trackId") or apple_id)
        image_url = clean_text(item.get("artworkUrl100")).replace("100x100bb", "600x600bb")
        return ExternalMusicTrack(
            source="apple_music",
            source_name="Apple Music",
            source_id=f"apple_{source_id}",
            title=title,
            artist=artist or "Unknown artist",
            url=url,
            image_url=image_url or None,
        )

    async def _resolve_oembed(
        self,
        url: str,
        endpoint_template: str,
        source: str,
        source_name: str,
    ) -> ExternalMusicTrack | None:
        endpoint = endpoint_template.format(url=quote(url, safe=""))
        async with self.session.get(endpoint, headers={"Accept": "application/json"}) as response:
            if response.status >= 400:
                return None
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            return None

        title = clean_text(data.get("title"))
        artist = clean_text(data.get("author_name"))
        parsed_title, parsed_artist = split_title_artist(title, source)
        if parsed_title:
            title = parsed_title
        if not artist and parsed_artist:
            artist = parsed_artist
        if not title or is_generic_metadata(title, artist):
            return None

        return ExternalMusicTrack(
            source=source,
            source_name=source_name,
            source_id=f"{source}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]}",
            title=title,
            artist=artist or "Unknown artist",
            url=url,
            image_url=best_oembed_image(data),
        )

    async def _fetch(self, url: str) -> str:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
        }
        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status >= 400:
                raise ExternalMusicResolverError(f"metadata page returned HTTP {response.status}")
            return await response.text(errors="ignore")

    async def _fetch_yandex_crawler(self, url: str) -> str:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "User-Agent": "TelegramBot (like TwitterBot)",
        }
        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status >= 400:
                raise ExternalMusicResolverError(f"metadata page returned HTTP {response.status}")
            return await response.text(errors="ignore")


def source_from_url(url: str) -> tuple[str, str]:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    if "spotify" in host:
        return "spotify", "Spotify"
    if "apple" in host:
        return "apple_music", "Apple Music"
    if "yandex" in host:
        return "yandex_music", "Yandex Music"
    if host in VK_MUSIC_HOSTS or host.endswith(".vk.com") or host.endswith(".vk.ru"):
        return "vk_music", "VK Music"
    if "soundcloud" in host:
        return "soundcloud", "SoundCloud"
    if "deezer" in host:
        return "deezer", "Deezer"
    if "tidal" in host:
        return "tidal", "TIDAL"
    if "bandcamp" in host:
        return "bandcamp", "Bandcamp"
    if "zvuk" in host:
        return "zvuk", "Zvuk"
    if "amazon" in host:
        return "amazon_music", "Amazon Music"
    if "qobuz" in host:
        return "qobuz", "Qobuz"
    if "audiomack" in host:
        return "audiomack", "Audiomack"
    if "anghami" in host:
        return "anghami", "Anghami"
    if "shazam" in host:
        return "shazam", "Shazam"
    if "genius" in host:
        return "genius", "Genius"
    if "last.fm" in host:
        return "lastfm", "Last.fm"
    if "pandora" in host:
        return "pandora", "Pandora"
    if host in {"song.link", "odesli.co", "lnk.to", "ffm.to", "found.ee", "linkfire.com"}:
        return "smart_link", "Music link"
    return "external_music", host or "Music"


def parse_title_artist(
    url: str,
    meta: dict[str, str],
    page_title: str,
    structured_title: str = "",
    structured_artist: str = "",
) -> tuple[str, str]:
    host = urlparse(url).netloc.lower()
    title = structured_title or (
        first_meta(
            meta,
            "music:song",
            "music:song:title",
            "og:audio:title",
            "og:title",
            "twitter:title",
            "title",
        )
        or page_title
    )
    artist = structured_artist or first_meta(
        meta,
        "music:musician",
        "music:creator",
        "music:artist",
        "og:audio:artist",
        "twitter:audio:artist_name",
        "byl",
        "author",
    )

    title = strip_service_noise(title)
    artist = strip_service_noise(artist)

    parsed_title, parsed_artist = split_title_artist(title, host)
    if not artist and parsed_artist:
        artist = parsed_artist
    if parsed_title:
        title = parsed_title

    return clean_text(title), clean_text(artist)


def first_meta(meta: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = meta.get(key.lower())
        if value:
            return value
    return ""


def best_image(meta: dict[str, str]) -> str | None:
    for key in ("og:image", "twitter:image", "twitter:image:src", "image"):
        value = meta.get(key)
        if value and value.startswith(("http://", "https://")):
            return value
    return None


def best_oembed_image(data: dict[str, object]) -> str | None:
    for key in ("thumbnail_url", "image", "artwork_url"):
        value = clean_text(data.get(key))
        if value.startswith(("http://", "https://")):
            return value
    return None


def yandex_ids_from_path(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    track_id = ""
    album_id = ""
    for index, part in enumerate(parts):
        if part == "track" and index + 1 < len(parts):
            track_id = numeric_prefix(parts[index + 1])
        elif part == "album" and index + 1 < len(parts):
            album_id = numeric_prefix(parts[index + 1])
    return track_id, album_id


def numeric_prefix(value: str) -> str:
    match = re.match(r"\d+", value or "")
    return match.group(0) if match else ""


def yandex_artist_from_description(value: str) -> str:
    value = clean_text(value)
    if not value or is_generic_metadata(value, ""):
        return ""
    if "•" in value:
        artist = clean_text(value.split("•", 1)[0])
        if artist and not is_generic_metadata("", artist):
            return artist
    match = re.search(r"Слушать\s+трек\s+(.+?)\s+.+?\s+онлайн", value, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def parse_yandex_page_title(value: str) -> tuple[str, str]:
    value = clean_text(value)
    if not value or is_generic_metadata(value, ""):
        return "", ""
    value = re.sub(r"\s+слушать\s+онлайн\s+на\s+Яндекс\s+Музыке$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+\|\s+Яндекс\s+Музыка$", "", value, flags=re.IGNORECASE)
    return clean_text(value), ""


def yandex_artist_from_state(html_text: str, track_id: str) -> str:
    block = yandex_state_block(html_text, track_id)
    if not block:
        return ""
    artists_match = re.search(r'"artists"\s*:\s*\[(.*?)\]', block, flags=re.DOTALL)
    if not artists_match:
        return ""
    names = [
        decode_json_fragment(item)
        for item in re.findall(r'"name"\s*:\s*"((?:\\.|[^"\\])*)"', artists_match.group(1))
    ]
    return ", ".join(filter(None, names))


def yandex_cover_from_state(html_text: str, track_id: str) -> str | None:
    block = yandex_state_block(html_text, track_id)
    if not block:
        return None
    match = re.search(r'"coverUri"\s*:\s*"((?:\\.|[^"\\])*)"', block)
    if not match:
        return None
    return yandex_cover_url(decode_json_fragment(match.group(1)))


def yandex_state_block(html_text: str, track_id: str) -> str:
    if not track_id:
        return ""
    marker = f'"id":"{track_id}"'
    index = html_text.find(marker)
    if index < 0:
        marker = f'"id":{track_id}'
        index = html_text.find(marker)
    if index < 0:
        return ""
    return html_text[max(0, index - 12_000) : index + 12_000]


def yandex_cover_url(value: str) -> str | None:
    value = clean_text(value).replace("\\/", "/")
    if not value:
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    elif not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value.replace("%%", "1000x1000")


def decode_json_fragment(value: str) -> str:
    try:
        return clean_text(json.loads(f'"{value}"'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return clean_text(value.replace("\\u002F", "/"))


def is_generic_metadata(title: str, artist: str) -> bool:
    normalized_title = clean_text(title).casefold()
    normalized_artist = clean_text(artist).casefold()
    generic_titles = {
        "hear the world's sounds",
        "hear the world\u2019s sounds",
        "soundcloud",
        "apple music",
        "yandex music",
        "яндекс музыка",
        "яндекс музыка — собираем музыку для вас",
        "яндекс музыка - собираем музыку для вас",
        "собираем музыку для вас",
        "собираем музыку для вас — яндекс музыка",
        "собираем музыку для вас - яндекс музыка",
        "поиск музыки, подкастов и аудиокниг | яндекс музыка",
        "vk music",
        "deezer",
        "tidal",
        "music",
    }
    if normalized_title in generic_titles:
        return True
    return normalized_title == normalized_artist and normalized_title in generic_titles


def split_title_artist(value: str, host: str) -> tuple[str, str]:
    value = clean_text(value)
    if not value:
        return "", ""

    by_match = re.search(r"^(.+?)\s+by\s+(.+?)(?:\s+on\s+.+)?$", value, flags=re.IGNORECASE)
    if by_match:
        return clean_text(by_match.group(1)), clean_text(by_match.group(2))

    if "genius.com" in host and value.lower().endswith(" lyrics"):
        value = value[:-7]

    for separator in (" - ", "\u2013", "\u2014", " | "):
        if separator not in value:
            continue
        left, right = [clean_text(item) for item in value.split(separator, 1)]
        if not left or not right:
            continue
        if "apple" in host or "yandex" in host or "deezer" in host or "tidal" in host:
            return left, right
        return right, left

    return value, ""


def parse_json_ld_music(chunks: list[str]) -> dict[str, str]:
    for chunk in chunks:
        try:
            payload = json.loads(chunk.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for node in iter_json_ld_nodes(payload):
            node_type = node.get("@type") or node.get("type") or ""
            if isinstance(node_type, list):
                type_value = " ".join(str(item) for item in node_type)
            else:
                type_value = str(node_type)
            if not any(marker in type_value.lower() for marker in ("musicrecording", "song")):
                continue

            title = clean_text(node.get("name"))
            artist = clean_text(
                entity_name(node.get("byArtist"))
                or entity_name(node.get("artist"))
                or entity_name(node.get("creator"))
            )
            image = entity_image(node.get("image"))
            if title:
                return {"title": title, "artist": artist, "image": image}
    return {}


def iter_json_ld_nodes(value: object):
    if isinstance(value, dict):
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from iter_json_ld_nodes(item)
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_ld_nodes(item)


def entity_name(value: object) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    if isinstance(value, list):
        return ", ".join(filter(None, (entity_name(item) for item in value)))
    return clean_text(value)


def entity_image(value: object) -> str:
    if isinstance(value, dict):
        return entity_image(value.get("url") or value.get("contentUrl"))
    if isinstance(value, list):
        for item in value:
            image = entity_image(item)
            if image:
                return image
        return ""
    value = clean_text(value)
    return value if value.startswith(("http://", "https://")) else ""


def strip_service_noise(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    patterns = (
        r"\s+on\s+Apple Music$",
        r"\s+\|\s+Apple Music$",
        r"\s+\|\s+Yandex Music$",
        r"\s+\|\s+Яндекс Музыка$",
        r"\s+\|\s+VK Music$",
        r"\s+\|\s+SoundCloud$",
        r"\s+\|\s+Deezer$",
        r"\s+\|\s+TIDAL$",
        r"\s+\|\s+Bandcamp$",
        r"\s+\|\s+Genius Lyrics$",
        r"\s+-\s+YouTube Music$",
        r"\s+на\s+Яндекс Музыке$",
        r"\s+Lyrics$",
    )
    for pattern in patterns:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return clean_text(value)


def clean_text(value: str | None) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value.replace("\n", " ")).strip()
    return value.strip(" -|\"'\u201c\u201d")
