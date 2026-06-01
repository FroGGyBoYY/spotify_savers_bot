from __future__ import annotations

from .locales import ar, en, es, ru, th, zh
from .emojis import EMOJI_CHARS, custom_emoji


SUPPORTED_LANGS = ("ru", "en", "es", "zh", "ar", "th")

TEXTS: dict[str, dict[str, str]] = {
    "ru": ru.TEXTS,
    "en": en.TEXTS,
    "es": es.TEXTS,
    "zh": zh.TEXTS,
    "ar": ar.TEXTS,
    "th": th.TEXTS,
}


class SafeFormatDict(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def pick_lang(value: str | None) -> str:
    if not value:
        return "ru"
    lang = value.lower().replace("_", "-").split("-")[0]
    return lang if lang in SUPPORTED_LANGS else "ru"


def t(lang: str | None, key: str, **kwargs: object) -> str:
    selected = pick_lang(lang)
    template = (
        TEXTS.get(selected, {}).get(key)
        or TEXTS["ru"].get(key)
        or TEXTS["en"].get(key)
        or key
    )
    values = SafeFormatDict({name: custom_emoji(name) for name in EMOJI_CHARS})
    values.update(kwargs)
    return template.format_map(values)
