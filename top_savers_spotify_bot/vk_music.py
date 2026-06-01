from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs

import aiohttp


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class VkMusicTrack:
    source_id: str
    title: str
    artist: str
    image_url: str | None = None


class VkMusicMetadataResolver:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str = "",
        api_version: str = "5.131",
        browser_metadata_enabled: bool = False,
        browser_timeout_seconds: int = 18,
    ) -> None:
        self.session = session
        self.access_token = access_token.strip()
        self.api_version = api_version
        self.browser_metadata_enabled = browser_metadata_enabled
        self.browser_timeout_seconds = max(5, browser_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.access_token) or self.browser_metadata_enabled

    async def resolve(self, parsed) -> VkMusicTrack | None:
        owner_id, audio_id = vk_audio_ids_from_url(parsed)
        if not owner_id or not audio_id:
            return None

        if self.access_token:
            api_track = await self._resolve_api(owner_id, audio_id)
            if api_track is not None:
                return api_track

        if self.browser_metadata_enabled:
            return await self._resolve_browser(parsed.geturl(), owner_id, audio_id)

        return None

    async def _resolve_api(self, owner_id: str, audio_id: str) -> VkMusicTrack | None:
        data = await self._audio_get_by_id(owner_id, audio_id)
        if not isinstance(data, dict) or data.get("error"):
            error = data.get("error") if isinstance(data, dict) else None
            if error:
                logger.info("VK audio metadata API returned error: %s", error)
            return None

        result = data.get("response")
        if not isinstance(result, list) or not result:
            return None
        item = next((entry for entry in result if isinstance(entry, dict)), None)
        if not item:
            return None

        title = clean_text(item.get("title"))
        artist = clean_text(item.get("artist"))
        if not title:
            return None

        return VkMusicTrack(
            source_id=f"vk_{owner_id}_{audio_id}",
            title=title,
            artist=artist or "Unknown artist",
            image_url=vk_cover_url(item),
        )

    async def _resolve_browser(self, url: str, owner_id: str, audio_id: str) -> VkMusicTrack | None:
        try:
            from playwright.async_api import async_playwright
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:
            logger.info("VK browser metadata is enabled, but playwright is not installed")
            return None

        timeout_ms = self.browser_timeout_seconds * 1000
        browser = None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=("--no-sandbox", "--disable-dev-shm-usage"),
                )
                context = await browser.new_context(
                    locale="ru-RU",
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                with suppress_playwright_timeout(PlaywrightTimeoutError):
                    await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
                with suppress_playwright_timeout(PlaywrightTimeoutError):
                    await page.wait_for_function(_VK_CARD_READY_SCRIPT, timeout=timeout_ms)

                payload = await page.evaluate(_VK_METADATA_SCRIPT)
                await context.close()
        except Exception as error:
            logger.info("VK browser metadata failed: %s", error)
            return None
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

        title, artist = title_artist_from_browser_payload(payload)
        if not title:
            return None
        return VkMusicTrack(
            source_id=f"vk_{owner_id}_{audio_id}",
            title=title,
            artist=artist or "Unknown artist",
            image_url=best_browser_image(payload),
        )

    async def _audio_get_by_id(self, owner_id: str, audio_id: str) -> dict[str, object] | None:
        try:
            async with self.session.get(
                "https://api.vk.com/method/audio.getById",
                params={
                    "audios": f"{owner_id}_{audio_id}",
                    "access_token": self.access_token,
                    "v": self.api_version,
                },
                headers={"Accept": "application/json"},
            ) as response:
                if response.status >= 400:
                    logger.info("VK audio metadata API returned HTTP %s", response.status)
                    return None
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, OSError, ValueError, json.JSONDecodeError) as error:
            logger.info("VK audio metadata API failed: %s", error)
            return None

        return payload if isinstance(payload, dict) else None


def vk_audio_ids_from_url(parsed) -> tuple[str, str]:
    path = parsed.path.strip("/")
    match = re.match(r"audio(-?\d+)_(\d+)$", path)
    if match:
        return match.group(1), match.group(2)

    query = parse_qs(parsed.query)
    owner_id = (query.get("owner_id") or [""])[0].strip()
    audio_id = (query.get("audio_id") or [""])[0].strip()
    if owner_id and audio_id:
        return owner_id, audio_id

    z_value = (query.get("z") or [""])[0].strip()
    match = re.search(r"audio(-?\d+)_(\d+)", z_value)
    if match:
        return match.group(1), match.group(2)

    return "", ""


def vk_cover_url(item: dict[str, object]) -> str | None:
    album = item.get("album")
    if not isinstance(album, dict):
        return None
    thumb = album.get("thumb")
    if not isinstance(thumb, dict):
        return None
    for key in ("photo_1200", "photo_600", "photo_300", "photo_270", "photo_135"):
        value = clean_text(thumb.get(key))
        if value.startswith(("http://", "https://")):
            return value
    return None


def title_artist_from_browser_payload(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list):
        return "", ""
    lines = [clean_text(item) for item in raw_lines if clean_text(item)]
    if not lines:
        return "", ""

    for index, line in enumerate(lines):
        if not is_listen_button_line(line):
            continue
        previous = [item for item in lines[:index] if not is_vk_noise_line(item)]
        if len(previous) >= 2:
            return previous[-2], previous[-1]

    for index in range(len(lines) - 1):
        current = lines[index]
        next_line = lines[index + 1]
        if current and next_line and not is_vk_noise_line(current) and not is_vk_noise_line(next_line):
            if current == next_line:
                continue
            if current in lines[:index]:
                return current, next_line
    return "", ""


def best_browser_image(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    images = payload.get("images")
    if not isinstance(images, list):
        return None

    best_url = ""
    best_score = -1.0
    for item in images:
        if not isinstance(item, dict):
            continue
        url = clean_text(item.get("src"))
        if not url.startswith(("http://", "https://")) or is_vk_noise_image(url):
            continue
        width = safe_float(item.get("width"))
        height = safe_float(item.get("height"))
        if width < 80 or height < 80:
            continue
        area = width * height
        square_bonus = 2.0 if min(width, height) / max(width, height) >= 0.7 else 1.0
        top_bonus = 1.5 if 0 <= safe_float(item.get("top")) <= 900 else 1.0
        score = area * square_bonus * top_bonus
        if score > best_score:
            best_score = score
            best_url = url
    return best_url or None


def is_listen_button_line(value: str) -> bool:
    lowered = value.casefold()
    labels = (
        "\u0441\u043b\u0443\u0448\u0430\u0442\u044c",
        "listen",
        "play",
    )
    return any(label in lowered for label in labels) and len(value) <= 40


def is_vk_noise_line(value: str) -> bool:
    lowered = value.casefold()
    noise = {
        "\u0432\u043e\u0439\u0442\u0438",
        "\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044f",
        "\u0433\u043b\u0430\u0432\u043d\u0430\u044f",
        "\u043c\u0443\u0437\u044b\u043a\u0430",
        "\u0432\u0438\u0434\u0435\u043e",
        "\u0441\u043e\u043e\u0431\u0449\u0435\u0441\u0442\u0432\u0430",
        "\u043c\u0438\u043d\u0438-\u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u044f",
        "\u0438\u0433\u0440\u044b",
        "\u0435\u0449\u0451",
        "\u0435\u0449\u0435",
        "vk",
        "vkontakte",
        "log in",
        "sign up",
        "music",
        "video",
        "games",
        "more",
    }
    return lowered in noise or is_listen_button_line(value)


def is_vk_noise_image(url: str) -> bool:
    lowered = url.casefold()
    return any(
        marker in lowered
        for marker in (
            "favicon",
            "icons",
            "logo",
            "emoji",
            "sticker",
            "blank",
            "camera",
        )
    )


def safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class suppress_playwright_timeout:
    def __init__(self, timeout_error_type: type[BaseException]) -> None:
        self.timeout_error_type = timeout_error_type

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return exc_type is not None and issubclass(exc_type, self.timeout_error_type)


def clean_text(value: object) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value.replace("\n", " ")).strip()
    return value.strip(" -|\"'\u201c\u201d")


_VK_CARD_READY_SCRIPT = """
() => {
  const text = document.body ? document.body.innerText || "" : "";
  return /\\u0421\\u043b\\u0443\\u0448\\u0430\\u0442\\u044c|Listen|Play/i.test(text);
}
"""


_VK_METADATA_SCRIPT = """
() => {
  const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const bodyText = document.body ? document.body.innerText || "" : "";
  const lines = bodyText.split(/\\n+/).map(clean).filter(Boolean);
  const imageItems = [];

  const addImage = (src, rect, alt) => {
    src = clean(src);
    if (!src || !/^https?:\\/\\//i.test(src)) return;
    imageItems.push({
      src,
      width: rect ? rect.width || 0 : 0,
      height: rect ? rect.height || 0 : 0,
      top: rect ? rect.top || 0 : 0,
      alt: clean(alt),
    });
  };

  for (const img of Array.from(document.images || [])) {
    addImage(img.currentSrc || img.src, img.getBoundingClientRect(), img.alt);
  }

  for (const element of Array.from(document.querySelectorAll("*"))) {
    const rect = element.getBoundingClientRect();
    if (!rect || rect.width < 80 || rect.height < 80) continue;
    const bg = window.getComputedStyle(element).backgroundImage || "";
    const match = bg.match(/url\\(["']?(.+?)["']?\\)/);
    if (match) addImage(match[1], rect, element.getAttribute("aria-label") || "");
  }

  return { lines, images: imageItems };
}
"""
