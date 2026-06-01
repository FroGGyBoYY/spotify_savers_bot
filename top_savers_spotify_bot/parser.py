from __future__ import annotations

import re
from dataclasses import dataclass


SPOTIFY_ID = r"(?:[A-Za-z0-9]\s*){22}"
URL_RE = re.compile(
    rf"open\.spotify\.com/(?:intl-[a-z]{{2}}/)?(?P<kind>track|album|playlist)/\s*(?P<id>{SPOTIFY_ID})",
    re.IGNORECASE,
)
URI_RE = re.compile(rf"spotify:(?P<kind>track|album|playlist):\s*(?P<id>{SPOTIFY_ID})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SpotifyLink:
    kind: str
    id: str


def parse_spotify_link(text: str) -> SpotifyLink | None:
    for pattern in (URI_RE, URL_RE):
        match = pattern.search(text)
        if match:
            spotify_id = re.sub(r"\s+", "", match.group("id"))
            return SpotifyLink(kind=match.group("kind").lower(), id=spotify_id)
    return None
