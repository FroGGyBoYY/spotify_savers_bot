from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .spotify import SpotifyTrack


SUPPORTED_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav")


class AudioLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def find_track(self, track: SpotifyTrack) -> Path | None:
        if not self.root.exists():
            return None

        direct = self._find_by_id(track.id)
        if direct:
            return direct

        wanted = {
            _normalize(f"{track.artist_names} - {track.name}"),
            _normalize(f"{track.name} - {track.artist_names}"),
        }

        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS:
                if _normalize(path.stem) in wanted:
                    return path
        return None

    def _find_by_id(self, track_id: str) -> Path | None:
        for extension in SUPPORTED_AUDIO_EXTENSIONS:
            candidate = self.root / f"{track_id}{extension}"
            if candidate.exists():
                return candidate
        return None


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", " ", value).lower()
    return " ".join(value.split())
