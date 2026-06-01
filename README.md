# Top Savers Spotify Bot

Telegram-бот брат для `@Top_Savers_bot`: принимает ссылки Spotify на треки, альбомы и плейлисты, вытаскивает метаданные через официальный Spotify Web API, отправляет аудио из вашей локальной легальной библиотеки или из одобренного лицензированного backend endpoint, кеширует отправленные аудио через Telegram `file_id`, ведет внутреннюю SQLite-базу, поддерживает обязательные подписки, рекламу, друзей без рекламы, админскую рассылку и 6 языков интерфейса.

Важно: бот не обходит DRM Spotify и не скачивает треки напрямую из публичного Spotify Web API. Spotify используется для ссылок, обложек и метаданных. Аудиофайлы бот отправляет только из вашей локальной легальной библиотеки (`AUDIO_LIBRARY_DIR`) или из вашего официально разрешенного backend (`AUTHORIZED_AUDIO_API_URL`). Если источника аудио нет, бот покажет карточку трека и кнопку открытия Spotify.

## Быстрый запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python main.py
```

Минимально нужны:

- `BOT_TOKEN` от BotFather
- `EXPECTED_BOT_USERNAME=@spotify_savers_bot` чтобы случайно не запустить старый токен
- `SPOTIFY_CLIENT_ID` и `SPOTIFY_CLIENT_SECRET` из [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

## Локальная библиотека полных треков

Положите свои легальные аудиофайлы в папку из `AUDIO_LIBRARY_DIR`.

Бот ищет файл по Spotify track id:

```text
library/3n3Ppam7vgaVa1iaRUc9Lp.mp3
```

И по человекочитаемым именам:

```text
library/Artist - Track.mp3
library/Track - Artist.flac
```

Поддерживаемые расширения: `.mp3`, `.m4a`, `.flac`, `.ogg`, `.opus`, `.wav`.

Каждый отправленный аудиофайл получает:

- `title` из Spotify
- `performer` из Spotify
- thumbnail-обложку, подготовленную для Telegram
- caption с подписью `💕 @spotify_savers_bot`

Для альбомов бот отправляет первые `FREE_ALBUM_TRACK_LIMIT` треков. Если пользователь включен как premium во внутренней SQLite-базе, лимит расширяется до `PREMIUM_ALBUM_TRACK_LIMIT`. Название альбома добавляется только в caption последнего отправленного трека альбома.

Если аудиофайл не найден в `library` и не подключен `AUTHORIZED_AUDIO_API_URL`, бот показывает кнопки поиска легального источника: YouTube Music, Apple Music, SoundCloud, Bandcamp, Jamendo и Internet Archive. Это matching-слой по `artist + title`; он не обходит DRM и не скачивает аудио с платформ, которые не дают официального права на скачивание.

## Официальный audio backend

Если у вас есть внутренний/партнерский endpoint, которому разрешено выдавать аудиофайлы для Telegram-интеграции, заполните:

```env
AUTHORIZED_AUDIO_API_URL=https://your-official-backend.example.com/telegram/audio
AUTHORIZED_AUDIO_API_URLS=https://backend1.example.com/telegram/audio,https://backend2.example.com/telegram/audio
AUTHORIZED_AUDIO_API_TOKEN=secret-token
```

Бот делает `POST` с JSON:

```json
{
  "spotify_id": "3n3Ppam7vgaVa1iaRUc9Lp",
  "title": "Track name",
  "artists": ["Artist"],
  "album": "Album name",
  "duration_ms": 180000,
  "spotify_url": "https://open.spotify.com/track/...",
  "preferred_quality": "max"
}
```

Endpoint может ответить одним из двух способов:

- `Content-Type: audio/*` и тело файла
- JSON `{"audio_url": "https://...", "cache_key": "optional-stable-key"}`

Полученный файл бот сохранит в `library/.authorized-cache/` и закеширует Telegram `file_id`, чтобы не загружать один и тот же трек повторно.

Если задан `AUTHORIZED_AUDIO_API_URLS`, бот пробует endpoint'ы по порядку ровно один раз: #1, затем #2, затем #3. При падении backend'а админам приходит уведомление, а при успешном фейловере в историю скачивания пишется источник вида `authorized_api:2`. Бесконечного цикла нет: если все endpoint'ы недоступны или не нашли файл, пользователь получает обычную ошибку/выбор кандидатов.

## YouTube Music matching

Для Spotify-ссылок бот всегда строит YouTube Music search URL вида `https://music.youtube.com/search?q=artist+title` и передает его в `AUTHORIZED_AUDIO_API_URL(S)` вместе с Spotify metadata. Поэтому базовый режим не требует `YOUTUBE_API_KEY`: поиск и выбор трека может делать ваш backend.

Опционально можно включить поиск кандидатов через YouTube Data API, чтобы сам Telegram-бот показывал первые 3 варианта с обложками:

```env
YOUTUBE_MUSIC_SEARCH_ENABLED=1
YOUTUBE_API_KEY=your-youtube-data-api-key
YOUTUBE_REGION_CODE=US
```

Если локального файла нет, бот передает backend поле `source_candidate`. В базовом режиме это search URL; при включенном YouTube Data API это может быть конкретный найденный трек. Пример для найденного кандидата:

```json
{
  "source_candidate": {
    "source": "youtube_music",
    "source_id": "youtube-video-id",
    "title": "Candidate title",
    "artist": "Channel name",
    "url": "https://music.youtube.com/watch?v=...",
    "thumbnail_url": "https://...",
    "score": 96,
    "is_exact": true
  }
}
```

Сам бот не скачивает аудио из YouTube Music напрямую. Он либо отправляет файл из `library`, либо берет файл у разрешенного backend, либо показывает пользователю варианты для выбора.

## Команды

- `/start` - старт и краткая инструкция
- `/help` - помощь
- `/lang ru|en|es|zh|ar|th` - сменить язык
- `/stats` - статистика, только админы
- `/broadcast текст` - рассылка всем пользователям, только админы
- `/premium user_id on|off` - включить/выключить расширенный альбомный лимит во внутренней базе, только админы
- `/admin user_id on|off` - добавить/убрать админа во внутренней базе
- `/friend user_id on|off [заметка]` - добавить/убрать друга без рекламы
- `/ad after_message|every_8h текст` - добавить рекламу после каждого сообщения или раз в 8 часов
- `/ads` - список реклам
- `/required telegram_channel|telegram_app|link значение [название]` - добавить обязательный канал/приложение/ссылку
- `/required_list` - список обязательных каналов/приложений/ссылок

## Группы

В личном чате бот принимает обычные музыкальные ссылки без команды.

В группах бот вызывается отдельными командами, чтобы не конфликтовать со старым `Top Savers` ботом:

```text
/music <ссылка>
/song <ссылка>
/track <ссылка>
```

Также можно ответить командой `/music`, `/song` или `/track` на сообщение со ссылкой, и бот возьмет ссылку из reply.

## Admin Panel

Быстрый вызов команды по номеру:

```text
/admin_run 1
```

Основные команды:

```text
/bot_status
/health_check
/users_count
/users_top
/top_downloads
/platform_stats
/cache_stats
/errors
/recent_downloads
/failed_downloads
/db_tables
/db_export
/table_export users
/ad_overview
/req_overview
/admin_list
/admin_add 123456789
/admin_del 123456789
/friend_add 123456789
/friend_del 123456789
/friend_list
/platform_health
/daily_report
/maintenance_on [text]
/maintenance_off
/maintenance_status
/ban 123456789
/unban 123456789
/banned
/reports
/ad8_list
/broadcast
/bc
/welcome_set
/welcome_status
/welcome_clear
/cleanup_temp 24
/users
/user 123456789
```

Реклама после скачивания:

1. Пришлите рекламное сообщение боту: текст, фото, видео, документ или медиа с caption.
2. Ответьте на него `/ad_add`.
3. С кнопками: `/ad_add Название | https://url | green || Вторая | https://url | red`.
4. Старый формат тоже работает: ответ на рекламу текстом `add Название | https://url | green`.
5. Управление: `/ad_list`, `/ad_on 1`, `/ad_off 1`, `/ad_del 1`, `/ad_stats`, `/ad_stats 1`, `/ad_stats_txt`.

Реклама каждые 8 часов:

1. Ответьте на рекламное сообщение `/ad8_add`.
2. С кнопками: `/ad8_add Название | https://url | green || Вторая | https://url | red`.
3. Управление: `/ad8_list`, `/ad8_on 1`, `/ad8_off 1`, `/ad8_del 1`, `/ad8_stats`, `/ad8_stats 1`, `/ad8_stats_txt`.

Broadcast:

1. Ответьте на любое сообщение `/broadcast`, и бот скопирует его всем пользователям.
2. С кнопками: `/broadcast Кнопка | https://url | green || Вторая | https://url | red`.
3. Короткая команда: `/bc`.

Telegram Bot API не дает задавать реальные цвета inline-кнопок, поэтому `green/blue/red/black` принимаются и сохраняются для совместимости с форматом, но внешний вид кнопок зависит от клиента Telegram.

## Внутренняя база

База отдельная и пока не связана с основной системой: `data/bot.sqlite3`.

Основные таблицы:

- `users` - Telegram user id, username/name, язык, статус подписки, premium-флаг, friend/admin-флаги, количество скачанных песен, время последнего скачивания
- `downloads` - история отправленных треков
- `file_cache` - Telegram `file_id` кеш
- `cached_tracks` - кеш треков: Spotify metadata, источник аудио, `file_id`, локальный путь
- `cached_albums` - кеш альбомов: metadata и список треков JSON
- `ads` - все рекламные сообщения, placement `after_message` или `every_8h`
- `ad_delivery_log` - кому и когда показывали рекламу; `every_8h` показывается фоновой задачей при работающем боте
- `required_links` - обязательные Telegram-каналы, приложения и ссылки
- `friends` - пользователи вообще без рекламы
- `admin_users` - админы бота

`ADMIN_IDS`, `NO_ADS_USER_IDS` и `REQUIRED_CHANNELS` из `.env` при запуске синхронизируются в эти таблицы.

## Возможности

- Spotify track/album/playlist links and `spotify:track:...` URI
- Telegram `file_id` cache for local/authorized files
- SQLite users/downloads/cache stats
- Internal premium flag for album limits
- Internal ads, required links, friends, and admins tables
- Required channel subscription checks
- Ads every N downloads
- No-ads user ids
- Admin broadcast
- 6 UI languages: Russian, English, Spanish, Chinese, Arabic, Thai
