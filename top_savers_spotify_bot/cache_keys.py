from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def canonical_track_key(title: str, artists: Iterable[str] | str, duration_ms: int = 0, isrc: str | None = None) -> str:
    normalized_isrc = normalize_text(isrc)
    if normalized_isrc:
        return f"isrc:v1:{normalized_isrc}"

    artist_key = "|".join(normalize_artists(artists))
    raw = f"{normalize_text(title)}|{artist_key}"
    if raw == "|":
        raw = f"unknown|{int(duration_ms or 0)}"
    return f"track:v1:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def canonical_source_alias_key(source: str, source_id: str) -> str:
    raw = f"{normalize_text(source)}|{normalize_text(source_id)}"
    return f"source:v1:{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def canonical_url_alias_key(url: str) -> str:
    return f"url:v1:{hashlib.sha1(normalize_url(url).encode('utf-8')).hexdigest()}"


def normalize_artists(artists: Iterable[str] | str) -> list[str]:
    if isinstance(artists, str):
        parts = re.split(r"\s*(?:,|&|\band\b|\bfeat\.?\b|\bft\.?\b)\s*", artists, flags=re.IGNORECASE)
    else:
        parts = list(artists)
    normalized = [normalize_text(item) for item in parts]
    return sorted({item for item in normalized if item})


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"[\[\(]\s*(official|audio|video|lyrics?|клип|премьера)\s*[\]\)]", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "ref_id", "si", "feature"}
    ]
    normalized = parsed._replace(
        scheme=parsed.scheme.lower() or "https",
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
        query=urlencode(sorted(query), doseq=True),
        fragment="",
    )
    return urlunparse(normalized)
