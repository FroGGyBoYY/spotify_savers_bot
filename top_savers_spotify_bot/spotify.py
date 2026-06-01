from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any

import aiohttp

from .config import Config


logger = logging.getLogger(__name__)


class SpotifyError(RuntimeError):
    pass


class SpotifyAuthError(SpotifyError):
    pass


class SpotifyPremiumRequired(SpotifyError):
    pass


class SpotifyNotFound(SpotifyError):
    pass


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    id: str
    name: str
    artists: tuple[str, ...]
    album: str
    duration_ms: int
    spotify_url: str
    image_url: str | None

    @property
    def artist_names(self) -> str:
        return ", ".join(self.artists) if self.artists else "Unknown artist"


@dataclass(frozen=True, slots=True)
class SpotifyCollection:
    kind: str
    id: str
    name: str
    total: int
    tracks: tuple[SpotifyTrack, ...]
    spotify_url: str
    image_url: str | None


class SpotifyClient:
    api_base = "https://api.spotify.com/v1"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._user_token: str | None = None
        self._user_token_expires_at = 0.0

    async def __aenter__(self) -> "SpotifyClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise SpotifyError("Spotify client session is not open")
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_token(self) -> str:
        if not self.config.spotify_client_id or not self.config.spotify_client_secret:
            raise SpotifyAuthError("Spotify credentials are missing")

        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        raw = f"{self.config.spotify_client_id}:{self.config.spotify_client_secret}".encode()
        auth = base64.b64encode(raw).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = {"grant_type": "client_credentials"}

        async with self.session.post(
            "https://accounts.spotify.com/api/token",
            headers=headers,
            data=data,
        ) as response:
            payload = await _json_response(response, "token")
            if response.status != 200:
                raise SpotifyAuthError(str(payload))

        self._token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    async def _ensure_user_token(self) -> str:
        if not self.config.spotify_client_id or not self.config.spotify_client_secret:
            raise SpotifyAuthError("Spotify credentials are missing")
        if not self.config.spotify_user_refresh_token:
            raise SpotifyAuthError("Spotify user refresh token is missing")

        if self._user_token and time.time() < self._user_token_expires_at - 30:
            return self._user_token

        raw = f"{self.config.spotify_client_id}:{self.config.spotify_client_secret}".encode()
        auth = base64.b64encode(raw).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.config.spotify_user_refresh_token,
        }

        async with self.session.post(
            "https://accounts.spotify.com/api/token",
            headers=headers,
            data=data,
        ) as response:
            payload = await _json_response(response, "user_token")
            if response.status != 200:
                raise SpotifyAuthError(str(payload))

        self._user_token = payload["access_token"]
        self._user_token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._user_token

    async def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._ensure_token()
        return await self._get_with_token(token, path_or_url, params=params)

    async def _get_user(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._ensure_user_token()
        return await self._get_with_token(token, path_or_url, params=params)

    async def _get_with_token(
        self,
        token: str,
        path_or_url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        async with self.session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        ) as response:
            payload = await _json_response(response, path_or_url)
            if response.status == 404:
                raise SpotifyNotFound(path_or_url)
            if response.status >= 400:
                raise SpotifyError(str(payload))
            return payload

    async def get_track(self, track_id: str) -> SpotifyTrack:
        return _track_from_payload(await self._get(f"/tracks/{track_id}"))

    async def get_album(self, album_id: str, limit: int) -> SpotifyCollection:
        album = await self._get(f"/albums/{album_id}")
        items = list(album.get("tracks", {}).get("items", []))
        next_url = album.get("tracks", {}).get("next")

        while next_url and len(items) < limit:
            page = await self._get(next_url)
            items.extend(page.get("items", []))
            next_url = page.get("next")

        image_url = _first_image(album)
        tracks = tuple(
            _track_from_payload(item, album_name=album.get("name", ""), image_url=image_url)
            for item in items[:limit]
        )
        return SpotifyCollection(
            kind="album",
            id=album["id"],
            name=album.get("name", ""),
            total=int(album.get("tracks", {}).get("total", len(items))),
            tracks=tracks,
            spotify_url=album.get("external_urls", {}).get("spotify", ""),
            image_url=image_url,
        )

    async def get_playlist(self, playlist_id: str, limit: int) -> SpotifyCollection:
        try:
            playlist = await self._get(f"/playlists/{playlist_id}")
        except SpotifyNotFound:
            if self.config.spotify_user_refresh_token:
                try:
                    playlist = await self._get_user(f"/playlists/{playlist_id}")
                    return await self._playlist_from_payload(playlist, limit, self._get_user)
                except SpotifyError as error:
                    logger.info(
                        "Spotify user-token playlist fallback failed for %s: %s",
                        playlist_id,
                        error,
                    )
            return await self._get_open_playlist(playlist_id, limit)

        collection = await self._playlist_from_payload(playlist, limit, self._get)
        if collection.total == 0 and self.config.spotify_user_refresh_token:
            try:
                playlist = await self._get_user(f"/playlists/{playlist_id}")
                return await self._playlist_from_payload(playlist, limit, self._get_user)
            except SpotifyError as error:
                logger.info(
                    "Spotify user-token empty-playlist fallback failed for %s: %s",
                    playlist_id,
                    error,
                )
        return collection

    async def _playlist_from_payload(
        self,
        playlist: dict[str, Any],
        limit: int,
        getter: Any,
    ) -> SpotifyCollection:
        tracks_payload = playlist.get("tracks") or playlist.get("items") or {}
        items = list(tracks_payload.get("items", [])) if isinstance(tracks_payload, dict) else []
        next_url = tracks_payload.get("next") if isinstance(tracks_payload, dict) else None

        while next_url and len(items) < limit:
            page = await getter(next_url)
            items.extend(page.get("items", []))
            next_url = page.get("next")

        tracks: list[SpotifyTrack] = []
        for item in items:
            track_payload = item.get("track") or item.get("item") if isinstance(item, dict) else None
            if not track_payload or track_payload.get("type") != "track":
                continue
            track = _track_from_payload(track_payload)
            if _is_generated_track_id(track.id):
                track = await self._recover_missing_track_id(track)
            tracks.append(track)
            if len(tracks) >= limit:
                break

        total = tracks_payload.get("total", len(items)) if isinstance(tracks_payload, dict) else len(items)
        return SpotifyCollection(
            kind="playlist",
            id=playlist["id"],
            name=playlist.get("name", ""),
            total=int(total or len(items)),
            tracks=tuple(tracks),
            spotify_url=playlist.get("external_urls", {}).get("spotify", ""),
            image_url=_first_image(playlist),
        )

    async def _recover_missing_track_id(self, track: SpotifyTrack) -> SpotifyTrack:
        if not track.name or not track.artists:
            return track

        queries = [
            f'track:"{track.name}" artist:"{track.artists[0]}"',
            f"{track.artists[0]} {track.name}",
        ]
        seen: set[str] = set()
        for query in queries:
            normalized_query = " ".join(query.split())
            if normalized_query in seen:
                continue
            seen.add(normalized_query)
            try:
                payload = await self._get(
                    "/search",
                    params={"q": normalized_query, "type": "track", "limit": 5},
                )
            except SpotifyError as error:
                logger.info("Spotify missing-track recovery search failed: %s", error)
                continue

            items = ((payload.get("tracks") or {}).get("items") or [])
            for item in items:
                try:
                    candidate = _track_from_payload(item)
                except (KeyError, TypeError, ValueError):
                    continue
                if _track_candidate_matches(track, candidate):
                    return replace(
                        track,
                        id=candidate.id,
                        album=candidate.album or track.album,
                        spotify_url=candidate.spotify_url or track.spotify_url,
                        image_url=candidate.image_url or track.image_url,
                        duration_ms=candidate.duration_ms or track.duration_ms,
                    )
        return track

    async def _get_open_playlist(self, playlist_id: str, limit: int) -> SpotifyCollection:
        url = f"https://open.spotify.com/playlist/{playlist_id}"
        async with self.session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise SpotifyNotFound(f"/playlists/{playlist_id}")

        match = re.search(r'<script id="initialState" type="text/plain">([^<]+)</script>', text)
        if not match:
            raise SpotifyNotFound(f"/playlists/{playlist_id}")

        raw = match.group(1)
        padded = raw + ("=" * (-len(raw) % 4))
        try:
            state = json.loads(base64.b64decode(padded).decode("utf-8", errors="replace"))
        except (ValueError, json.JSONDecodeError) as error:
            raise SpotifyNotFound(f"/playlists/{playlist_id}") from error

        entity = (
            state.get("entities", {})
            .get("items", {})
            .get(f"spotify:playlist:{playlist_id}")
        )
        if not isinstance(entity, dict):
            raise SpotifyNotFound(f"/playlists/{playlist_id}")

        content = entity.get("content") or {}
        items = content.get("items") or []
        tracks: list[SpotifyTrack] = []
        for item in items:
            data = ((item.get("itemV2") or {}).get("data") or {}) if isinstance(item, dict) else {}
            if data.get("__typename") != "Track":
                continue
            tracks.append(_track_from_open_payload(data))
            if len(tracks) >= limit:
                break

        return SpotifyCollection(
            kind="playlist",
            id=playlist_id,
            name=str(entity.get("name") or ""),
            total=int(content.get("totalCount") or len(items)),
            tracks=tuple(tracks),
            spotify_url=url,
            image_url=_first_open_image(entity),
        )

def _first_image(payload: dict[str, Any]) -> str | None:
    images = payload.get("images") or payload.get("album", {}).get("images") or []
    if not images:
        return None
    return images[0].get("url")


def _first_open_image(payload: dict[str, Any]) -> str | None:
    images = ((payload.get("images") or {}).get("items") or [])
    for item in images:
        sources = ((item or {}).get("sources") or [])
        if sources:
            return sources[0].get("url")
    return None


def _track_from_open_payload(payload: dict[str, Any]) -> SpotifyTrack:
    uri = str(payload.get("uri") or "")
    track_id = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else uri
    artists = tuple(
        str(((item or {}).get("profile") or {}).get("name") or "").strip()
        for item in ((payload.get("artists") or {}).get("items") or [])
        if str(((item or {}).get("profile") or {}).get("name") or "").strip()
    )
    album = payload.get("albumOfTrack") or {}
    duration = payload.get("duration") or {}
    return SpotifyTrack(
        id=track_id,
        name=str(payload.get("name") or ""),
        artists=artists,
        album=str(album.get("name") or ""),
        duration_ms=int(duration.get("totalMilliseconds") or 0),
        spotify_url=f"https://open.spotify.com/track/{track_id}" if track_id else "",
        image_url=_first_open_cover(album),
    )


def _first_open_cover(payload: dict[str, Any]) -> str | None:
    sources = ((payload.get("coverArt") or {}).get("sources") or [])
    if not sources:
        return None
    return sources[0].get("url")


async def _json_response(response: aiohttp.ClientResponse, context: str) -> dict[str, Any]:
    text = await response.text()
    if not text.strip():
        raise SpotifyError(f"Spotify returned empty response ({response.status}) for {context}")

    if response.status == 403 and "premium subscription required" in text.lower():
        raise SpotifyPremiumRequired(
            "Active Spotify Premium subscription is required for the owner of the app."
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        snippet = " ".join(text[:240].split())
        logger.warning(
            "Spotify returned non-JSON response status=%s context=%s body=%r",
            response.status,
            context,
            snippet,
        )
        raise SpotifyError(
            f"Spotify returned non-JSON response ({response.status}) for {context}"
        ) from error

    if not isinstance(payload, dict):
        raise SpotifyError(f"Spotify returned unexpected JSON ({response.status}) for {context}")
    return payload


def _track_from_payload(
    payload: dict[str, Any],
    album_name: str | None = None,
    image_url: str | None = None,
) -> SpotifyTrack:
    album = payload.get("album") or {}
    artists = tuple(artist.get("name", "") for artist in payload.get("artists", []) if artist.get("name"))
    duration_ms = int(payload.get("duration_ms", 0))
    return SpotifyTrack(
        id=_track_id_from_payload(payload, artists, duration_ms),
        name=payload.get("name", ""),
        artists=artists,
        album=album_name if album_name is not None else album.get("name", ""),
        duration_ms=duration_ms,
        spotify_url=payload.get("external_urls", {}).get("spotify", ""),
        image_url=image_url if image_url is not None else _first_image(payload),
    )


def _track_id_from_payload(payload: dict[str, Any], artists: tuple[str, ...], duration_ms: int) -> str:
    track_id = str(payload.get("id") or "").strip()
    if track_id:
        return track_id

    for key in ("uri", "href"):
        value = str(payload.get(key) or "").strip()
        match = re.search(r"(?:spotify:track:|/tracks/)([A-Za-z0-9]{22})", value)
        if match:
            return match.group(1)

    spotify_url = str((payload.get("external_urls") or {}).get("spotify") or "").strip()
    match = re.search(r"/track/([A-Za-z0-9]{22})", spotify_url)
    if match:
        return match.group(1)

    raw = "|".join(
        [
            str(payload.get("name") or ""),
            ",".join(artists),
            str(duration_ms or 0),
            spotify_url,
        ]
    )
    return f"generated:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def _is_generated_track_id(track_id: str) -> bool:
    return str(track_id or "").startswith("generated:")


def _track_candidate_matches(original: SpotifyTrack, candidate: SpotifyTrack) -> bool:
    if _normalize_for_match(original.name) != _normalize_for_match(candidate.name):
        return False

    original_artists = {_normalize_for_match(artist) for artist in original.artists if artist}
    candidate_artists = {_normalize_for_match(artist) for artist in candidate.artists if artist}
    if original_artists and candidate_artists and original_artists.isdisjoint(candidate_artists):
        return False

    if original.duration_ms and candidate.duration_ms:
        return abs(original.duration_ms - candidate.duration_ms) <= 10_000
    return True


def _normalize_for_match(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()
