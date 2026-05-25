# Загрузка картинок в MAX

Картинки, которые бот показывает часто, нужно заранее загружать в MAX и отправлять по `token`. Не передавайте локальные PNG через `InputMedia` в обычных меню: `maxapi` после загрузки такого медиа ждёт паузу перед отправкой сообщения, и пользователь видит задержку.

Текущие token-ы лежат в:

```text
app/assets/max-image-tokens.json
```

Код берёт их через `app.bot.assets.image_attachment(...)`. Если token для картинки отсутствует, есть запасной путь: бот отправит локальный файл как раньше. Этот fallback нужен для локальной разработки, но в продакшене лучше держать JSON актуальным.

## Загрузить стандартные картинки

Проверьте, что в `.env` или окружении есть `MAX_BOT_TOKEN`, затем выполните:

```powershell
python .\scripts\upload_max_assets.py
```

Скрипт загружает стандартные картинки:

- `app/assets/main-menu.png`;
- `app/assets/organizer-menu.png`;
- `app/assets/participants-menu.png`;
- `app/assets/notification-reminder.png`.

После загрузки он перезаписывает `app/assets/max-image-tokens.json` и печатает полученные token-ы.

## Загрузить новую картинку

Для одной или нескольких дополнительных картинок используйте формат `KEY=PATH`:

```powershell
python .\scripts\upload_max_assets.py --asset new_banner=app/assets/new-banner.png
```

Если нужно сохранить результат в другой файл:

```powershell
python .\scripts\upload_max_assets.py `
  --asset new_banner=app/assets/new-banner.png `
  --output outputs/max-image-tokens.json
```

Чтобы новая картинка использовалась ботом постоянно, добавьте ключ в `BotImageAsset` и `ASSET_PATHS` в `app/bot/assets.py`, затем перезапустите загрузку стандартного набора или перенесите token в основной JSON.

## Проверка после загрузки

После изменения token-ов выполните:

```powershell
pytest tests/test_bot_assets.py tests/test_bot_flow.py tests/test_notification_worker.py
```

Если менялся код приложения, затем соберите пакет Cloud Functions:

```powershell
.\scripts\build-yc-package.ps1
```

И проверьте, что JSON попал в пакет:

```powershell
rg "main_menu|organizer_menu|participants_menu|notification_reminder" dist/yc-package/app/assets/max-image-tokens.json
```

## Как это работает в MAX

MAX API для загрузки медиа работает в два шага: сначала `POST /uploads` возвращает URL загрузки, потом файл отправляется на этот URL. Для `type=image` token возвращается после загрузки изображения. Скрипт делает это через `maxapi.Bot.upload_media(...)`, поэтому руками вызывать HTTP-запросы обычно не нужно.

