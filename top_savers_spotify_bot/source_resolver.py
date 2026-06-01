from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

from .spotify import SpotifyTrack


@dataclass(frozen=True, slots=True)
class SourceLink:
    name: str
    url: str


def track_search_query(track: SpotifyTrack) -> str:
    return f"{track.artist_names} {track.name}".strip()


def youtube_music_search_url(track: SpotifyTrack) -> str:
    return f"https://music.youtube.com/search?q={quote_plus(track_search_query(track))}"


def legal_source_links(track: SpotifyTrack) -> list[SourceLink]:
    query = quote_plus(track_search_query(track))
    return [
        SourceLink("YouTube Music", youtube_music_search_url(track)),
        SourceLink("Apple Music", f"https://music.apple.com/search?term={query}"),
        SourceLink("SoundCloud", f"https://soundcloud.com/search/sounds?q={query}"),
        SourceLink("Bandcamp", f"https://bandcamp.com/search?q={query}"),
        SourceLink("Jamendo", f"https://www.jamendo.com/search?qs=q={query}"),
        SourceLink("Internet Archive", f"https://archive.org/search?query={query}"),
    ]
