from __future__ import annotations

import hashlib
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from .config import Config
from .spotify import SpotifyTrack


class AuthorizedAudioProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedAudio:
    path: Path
    cache_key: str
    provider_index: int = 1
    failover_warnings: tuple[str, ...] = ()


class AuthorizedAudioProvider:
    """Adapter for an approved/licensed backend that returns track audio bytes."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AuthorizedAudioProvider":
        timeout = aiohttp.ClientTimeout(total=self.config.authorized_audio_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def enabled(self) -> bool:
        return bool(self.config.authorized_audio_api_urls)

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise AuthorizedAudioProviderError("Authorized audio provider session is not open")
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch(
        self,
        track: SpotifyTrack,
        source_candidate: Mapping[str, Any] | None = None,
    ) -> AuthorizedAudio | None:
        if not self.enabled:
            return None

        headers = {"Accept": "application/json, audio/*"}
        if self.config.authorized_audio_api_token:
            headers["Authorization"] = f"Bearer {self.config.authorized_audio_api_token}"

        payload = {
            "spotify_id": track.id,
            "title": track.name,
            "artists": list(track.artists),
            "album": track.album,
            "duration_ms": track.duration_ms,
            "spotify_url": track.spotify_url,
            "preferred_quality": "max",
        }
        if source_candidate:
            payload["source_candidate"] = dict(source_candidate)

        default_cache_key = f"authorized:{track.id}"
        if source_candidate:
            source = str(source_candidate.get("source") or "source")
            source_id = str(source_candidate.get("source_id") or source_candidate.get("id") or track.id)
            default_cache_key = f"authorized:{source}:{source_id}:{track.id}"

        warnings: list[str] = []
        not_found_count = 0
        endpoints = list(self.config.authorized_audio_api_urls)
        for index, endpoint_url in enumerate(endpoints, start=1):
            try:
                audio = await self._fetch_from_endpoint(
                    endpoint_url,
                    index,
                    track,
                    payload,
                    headers,
                    default_cache_key,
                    tuple(warnings),
                )
            except (AuthorizedAudioProviderError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                warnings.append(f"backend #{index}: {error_message(error)}")
                continue
            if audio is not None:
                return audio
            not_found_count += 1

        if warnings:
            raise AuthorizedAudioProviderError("; ".join(warnings))
        if not_found_count == len(endpoints):
            return None
        return None

    async def _fetch_from_endpoint(
        self,
        endpoint_url: str,
        provider_index: int,
        track: SpotifyTrack,
        payload: dict[str, Any],
        headers: dict[str, str],
        default_cache_key: str,
        failover_warnings: tuple[str, ...],
    ) -> AuthorizedAudio | None:
        async with self.session.post(
            endpoint_url,
            json=payload,
            headers=headers,
        ) as response:
            if response.status == 404:
                return None
            if response.status >= 400:
                raise AuthorizedAudioProviderError(
                    f"HTTP {response.status}: {await _error_response_message(response)}"
                )

            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("audio/") or content_type.startswith("application/octet-stream"):
                extension = _extension_from_content_type(content_type)
                cache_key = response.headers.get("X-Cache-Key") or default_cache_key
                return await self._save_audio(
                    track.id,
                    await response.read(),
                    extension,
                    cache_key,
                    provider_index,
                    failover_warnings,
                )

            data = await response.json(content_type=None)
            audio_url = str(data.get("audio_url", "")).strip()
            if not audio_url:
                return None
            cache_key = str(data.get("cache_key") or default_cache_key)
            return await self._download_audio(
                track.id,
                audio_url,
                cache_key,
                provider_index,
                failover_warnings,
            )

    async def _download_audio(
        self,
        track_id: str,
        audio_url: str,
        cache_key: str,
        provider_index: int,
        failover_warnings: tuple[str, ...],
    ) -> AuthorizedAudio:
        parsed = urlparse(audio_url)
        if parsed.scheme not in {"https", "http"}:
            raise AuthorizedAudioProviderError("Authorized audio API returned unsupported audio_url scheme")

        async with self.session.get(audio_url, headers={"Accept": "audio/*"}) as response:
            if response.status >= 400:
                raise AuthorizedAudioProviderError(f"Authorized audio URL returned {response.status}")
            content_type = response.headers.get("Content-Type", "").lower()
            extension = _extension_from_content_type(content_type)
            return await self._save_audio(
                track_id,
                await response.read(),
                extension,
                cache_key,
                provider_index,
                failover_warnings,
            )

    async def _save_audio(
        self,
        track_id: str,
        content: bytes,
        extension: str,
        cache_key: str,
        provider_index: int,
        failover_warnings: tuple[str, ...],
    ) -> AuthorizedAudio:
        if not content:
            raise AuthorizedAudioProviderError("Authorized audio API returned an empty file")

        digest = hashlib.sha256(content).hexdigest()[:16]
        directory = self.config.audio_library_dir / ".authorized-cache"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{track_id}-{digest}{extension}"
        if not path.exists():
            path.write_bytes(content)
        return AuthorizedAudio(
            path=path,
            cache_key=cache_key,
            provider_index=provider_index,
            failover_warnings=failover_warnings,
        )


def _extension_from_content_type(content_type: str) -> str:
    if "mpeg" in content_type or "mp3" in content_type:
        return ".mp3"
    if "mp4" in content_type or "m4a" in content_type:
        return ".m4a"
    if "flac" in content_type:
        return ".flac"
    if "ogg" in content_type:
        return ".ogg"
    if "opus" in content_type:
        return ".opus"
    if "wav" in content_type:
        return ".wav"
    return ".mp3"


async def _error_response_message(response: aiohttp.ClientResponse) -> str:
    try:
        payload = await response.json(content_type=None)
    except Exception:
        text = await response.text(errors="ignore")
        return " ".join(text[:300].split()) or "empty response"
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        return message or "empty error"
    return str(payload)[:300] or "empty error"


def error_message(error: BaseException) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__
