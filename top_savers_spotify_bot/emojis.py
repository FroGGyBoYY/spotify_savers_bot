from __future__ import annotations


CUSTOM_EMOJIS: dict[str, str] = {
    "spotify": "5463107823946717464",
    "youtube_music": "5334681713316479679",
    "music": "5463107823946717464",
    "info": "5334544901428229844",
    "error": "5974083768233760323",
    "warning": "5420323339723881652",
    "lock": "5291873529464122510",
    "limit": "6084515769780013003",
    "check": "5021905410089550576",
    "hourglass": "5386367538735104399",
    "download": "4929524417354007168",
    "heart": "5328014489554002336",
    "double_heart": "5377676285864595613",
    "link": "5271604874419647061",
    "globe": "5330237710655306682",
    "quality": "5927026418616636353",
    "lang_ru": "5330499141019661433",
    "lang_en": "5174647435116414069",
    "lang_es": "5222024776976970940",
    "lang_ar": "5224565851427976312",
    "lang_zh": "5224435456220868088",
    "lang_th": "5294308565467543746",
}


EMOJI_CHARS: dict[str, str] = {
    "spotify": "🎧",
    "youtube_music": "▶️",
    "music": "🎧",
    "info": "ℹ️",
    "error": "❌",
    "warning": "⚠️",
    "lock": "🔒",
    "limit": "⛔",
    "check": "✅",
    "hourglass": "⏳",
    "download": "📥",
    "heart": "❤️",
    "double_heart": "💕",
    "link": "🔗",
    "globe": "🌐",
    "quality": "📺",
    "lang_ru": "🇷🇺",
    "lang_en": "🇬🇧",
    "lang_es": "🇪🇸",
    "lang_ar": "🇸🇦",
    "lang_zh": "🇨🇳",
    "lang_th": "🇹🇭",
}


def emoji_id(name: str) -> str | None:
    return CUSTOM_EMOJIS.get(name) or None


def custom_emoji(name: str) -> str:
    char = EMOJI_CHARS.get(name, "")
    custom_id = emoji_id(name)
    if not char:
        return ""
    if not custom_id:
        return char
    return f'<tg-emoji emoji-id="{custom_id}">{char}</tg-emoji>'
