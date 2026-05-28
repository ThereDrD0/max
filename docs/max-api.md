# Работа с MAX API

Бот общается с MAX через Bot API. В коде используется Python-библиотека `maxapi`, но проект не отдает ей всю архитектуру: у нас есть свой adapter в `app/bot/client.py`, свой dispatcher и свои обработчики.

## Где MAX API используется в коде

Основные файлы:

- `app/bot/client.py` — адаптер над `maxapi.Bot`;
- `app/bot/dispatcher.py` — разбор входящих update-событий;
- `app/bot/handlers.py` — пользовательские сценарии;
- `app/bot/keyboards.py` — inline-кнопки;
- `app/bot/payloads.py` — compact payload для callback-кнопок;
- `app/bot/deeplinks.py` — ссылки на конкретные мероприятия;
- `app/bot/assets.py` — картинки меню и уведомлений.

`MaxApiBotClient` умеет отправлять сообщения, получать ник бота и отправлять вложения в формате, который ожидает библиотека `maxapi`.

## Почему webhook, а не long polling

Webhook — это публичный HTTPS-адрес, на который MAX сам присылает события бота. Long polling — это когда приложение само постоянно опрашивает MAX.

Для этого проекта выбран webhook:

- Cloud Functions просыпается только на входящее событие;
- не нужен постоянно работающий процесс;
- timer trigger отдельно решает фоновые уведомления;
- MAX быстрее доставляет пользовательские действия в serverless-сценарии.

Long polling удобен для некоторых локальных экспериментов, но плохо подходит Cloud Functions: функция не должна бесконечно ждать новые события.

## Подписка MAX

Официальная документация MAX по подпискам:

- [POST /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions)
- [GET /subscriptions](https://dev.max.ru/docs-api/methods/GET/subscriptions)

Создать подписку:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-domain.example/webhook",
    "update_types": ["bot_started", "message_created", "message_callback"],
    "secret": "change_me_secret"
  }'
```

Для Yandex Cloud Functions URL обычно без `/webhook`:

```text
https://functions.yandexcloud.net/<function_id>
```

Для своего FastAPI-сервера:

```text
https://bot.example.com/webhook
```

Проверить подписки:

```bash
curl -X GET "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>"
```

Важные моменты из официальной документации MAX:

- URL webhook должен быть HTTPS;
- токен передается в заголовке `Authorization`;
- `secret` MAX присылает в заголовке `X-Max-Bot-Api-Secret`;
- если webhook долго не отвечает успешно, MAX будет повторять доставку, а затем может удалить подписку.

## Webhook secret

`WEBHOOK_SECRET` — это не токен бота. Это отдельная строка, которая защищает webhook от чужих POST-запросов.

MAX отправляет:

```http
X-Max-Bot-Api-Secret: <secret>
```

Код сравнивает заголовок с `WEBHOOK_SECRET`:

- FastAPI-режим: `app/web.py`;
- Cloud Functions-режим: `app/function_handler.py`.

Если secret не совпадает, ответ будет `403`.

В продакшене `WEBHOOK_SECRET` должен быть задан. Пустой secret отключает проверку, это допустимо только для локальных экспериментов.

## Типы update-событий

Проект обрабатывает три типа:

| `update_type` | Что означает | Где используется |
| --- | --- | --- |
| `bot_started` | Пользователь запустил бота или перешел по диплинку. | Первый вход, дисклеймер, открытие мероприятия по ссылке. |
| `message_created` | Пользователь отправил текст. | Команды `/start`, `/events`, `/my`, `/organizer`, `/admin`, `/find КОД`. |
| `message_callback` | Пользователь нажал inline-кнопку. | Почти вся навигация по меню и действиям. |

Неизвестные события игнорируются. Это не ошибка: MAX может присылать типы, которые этому боту не нужны.

## Формат локального webhook

Пример `message_created`:

```json
{
  "update_type": "message_created",
  "message": {
    "sender": {
      "user_id": 101,
      "name": "Локальный пользователь"
    },
    "recipient": {
      "chat_id": 9001
    },
    "body": {
      "text": "/start",
      "mid": "local-user-message-1"
    }
  }
}
```

PowerShell:

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "X-Max-Bot-Api-Secret" = "change_me_secret"
}

$body = @{
  update_type = "message_created"
  message = @{
    sender = @{ user_id = 101; name = "Локальный пользователь" }
    recipient = @{ chat_id = 9001 }
    body = @{ text = "/start"; mid = "local-user-message-1" }
  }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8080/webhook" `
  -Headers $headers `
  -Body $body
```

Готовые скрипты:

```powershell
.\scripts\send-sample-update.ps1
```

```bash
bash scripts/send-sample-update.sh
```

## Callback payload

Inline-кнопки возвращают боту payload. В проекте payload — короткая строка:

```text
action|event_id|slot_id|registration_id|value
```

Примеры:

```text
event_detail|123
register_confirm|123||456
my_regs||||1
```

Код:

```text
app/bot/payloads.py
```

Зачем так: payload должен быть компактным и стабильным. В нем не нужно хранить весь объект мероприятия или записи; достаточно id и действия.

## Inline-кнопки

Кнопки создаются в `app/bot/keyboards.py`.

Обычная callback-кнопка:

```json
{
  "type": "callback",
  "text": "Записаться",
  "payload": "event_book|123",
  "intent": "default"
}
```

Кнопка копирования публичной ссылки:

```json
{
  "type": "clipboard",
  "text": "Поделиться",
  "payload": "https://max.ru/id123_bot?start=e_open-day"
}
```

MAX-клавиатура отправляется как attachment типа `inline_keyboard`.

## Диплинки на мероприятия

Формат:

```text
https://max.ru/<botName>?start=e_<event-slug>
```

Пример:

```text
https://max.ru/university_bot?start=e_open-day-2026-06-15
```

Правила:

- payload начинается с `e_`;
- slug состоит из латинских строчных букв, цифр и дефиса;
- длина payload ограничена MAX, поэтому slug должен быть коротким;
- slug хранится в `event_deeplinks`;
- если `MAX_BOT_USERNAME` пустой, бот пробует получить ник через MAX API;
- если slug отсутствует у старого мероприятия, бот может создать его при первом показе карточки.

Входящий `bot_started` с payload:

```json
{
  "update_type": "bot_started",
  "chat_id": 9001,
  "user": {
    "user_id": 101,
    "name": "Анна"
  },
  "payload": "e_open-day-2026-06-15"
}
```

## Отправка сообщений

Все исходящие сообщения идут через:

```python
MaxApiBotClient.send_message(...)
```

В текущем UX бот отправляет новое сообщение на действие пользователя. Он не хранит `last_bot_message_id`, не удаляет старые сообщения и не пытается редактировать предыдущее сообщение. Это сделано ради предсказуемости и скорости: меньше YDB-чтений и меньше MAX API-вызовов.

Чтобы быстрые повторные клики одного пользователя не перемешивали ответы, `BotHandlers._send` использует per-user lock. Per-user lock — это очередь отправки для одного пользователя внутри текущего event loop.

## Картинки

Часто используемые картинки меню не надо отправлять как локальные PNG через `InputMedia`. Их нужно заранее загрузить в MAX и отправлять по `token`.

Почему: загрузка локального файла на каждый ответ добавляет задержку и зависит от поведения библиотеки `maxapi`. В проекте для этого есть:

```text
docs/max-image-assets.md
scripts/upload_max_assets.py
app/assets/max-image-tokens.json
app/bot/assets.py
```

Загрузить стандартные картинки:

```powershell
python .\scripts\upload_max_assets.py
```

Если token отсутствует, есть fallback: бот отправит локальный файл. Это удобно локально, но в продакшене `input_media_count` в метриках должен быть `0`.

## Уведомления и MAX API

Пользовательские webhook-действия не должны отправлять массовые уведомления в цикле. Для этого есть `notification_outbox`.

`NotificationWorker`:

1. выбирает due-уведомления;
2. проверяет статус записи и флаг уведомлений;
3. отправляет сообщение через MAX API;
4. отмечает результат в YDB;
5. соблюдает `MAX_API_RPS`.

`MAX_API_RPS` — requests per second, то есть запросов в секунду. Если MAX опубликует другой лимит для вашего бота или окружения, значение нужно снизить.

## Частые ошибки

`403 Forbidden` от webhook.

Не совпадает `WEBHOOK_SECRET` в приложении и подписке MAX.

MAX не вызывает webhook.

URL не публичный, не HTTPS, подписка создана не на тот адрес или функция не отвечает успешным кодом.

Бот молчит после callback.

Проверьте, приходит ли `message_callback` в логи. Если приходит, смотрите `perf_metric`, stack trace и payload.

Публичная ссылка на мероприятие не появляется.

Проверьте `MAX_BOT_USERNAME`. Если переменная пустая, бот должен сходить в MAX API за ником, но лучше задать явно.

Сообщения с картинками стали медленными.

Проверьте `app/assets/max-image-tokens.json` и метрику `input_media_count`. Если она больше нуля, бот использует fallback-отправку локальных PNG.

Уведомления не уходят.

Проверьте `notification_outbox`, timer trigger, `MAX_API_RPS`, `MAX_BOT_TOKEN` и логи вызовов timer.
