# Запуск проекта с нуля

Этот документ ведет от пустой машины до работающего бота. Здесь собраны локальный запуск, переменные окружения и выбор хостинга: свой сервер или Yandex serverless.

## Что нужно установить

Минимум для локальной разработки:

- Python 3.12;
- Docker и Docker Compose;
- Git;
- PowerShell на Windows или обычная shell-оболочка на Linux;
- токен MAX-бота.

Для деплоя в Yandex Cloud дополнительно нужен `yc` — командная строка Yandex Cloud. Подробный деплой описан в [yandex-cloud.md](yandex-cloud.md).

## Вариант 1: локально через Docker

Это самый простой путь для первого запуска. Docker Compose поднимает локальную YDB и приложение.

Windows:

```powershell
Copy-Item .env.example .env
notepad .env
.\scripts\dev-up.ps1 -Build
```

Linux:

```bash
cp .env.example .env
nano .env
bash scripts/dev-up.sh --build
```

После старта:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
```

YDB UI:

```text
http://localhost:8765
```

Остановка:

```powershell
.\scripts\dev-down.ps1
```

```bash
bash scripts/dev-down.sh
```

Что происходит при запуске контейнера:

1. поднимается local-ydb;
2. бот ждет доступности YDB;
3. запускается `python -m app.ydb_schema`;
4. запускается `python -m app.seed`;
5. стартует `uvicorn app.main:app --host 0.0.0.0 --port 8080`.

## Вариант 2: локально без Docker

Этот путь полезен, если вы хотите запускать приложение прямо из виртуального окружения Python. YDB все равно должна быть доступна: либо local-ydb в Docker, либо облачная YDB.

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .[test,migration]
Copy-Item .env.example .env
notepad .env
python -m app.ydb_schema
python -m app.seed
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test,migration]'
cp .env.example .env
nano .env
python -m app.ydb_schema
python -m app.seed
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Если YDB запущена не на `localhost:2136`, поменяйте `YDB_ENDPOINT` и `YDB_DATABASE`.

## Проверка webhook без MAX

MAX не может вызвать `localhost` на вашей машине. Но локальную обработку можно проверить тестовым update:

```powershell
.\scripts\send-sample-update.ps1
```

```bash
bash scripts/send-sample-update.sh
```

Скрипт отправляет `bot_started` на `http://localhost:8080/webhook`.

Можно отправить событие руками:

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

Если ответ `{"ok":true}`, webhook обработал событие. Если при этом пользователь в MAX ничего не получил, это нормально для полностью локального теста без реального `MAX_BOT_TOKEN` и доступного MAX API.

## ENV-настройки

Шаблон:

```text
.env.example
```

Настройки читаются в `app/config.py` через `pydantic-settings`. `pydantic-settings` — библиотека, которая строит объект настроек из переменных окружения и `.env`.

Основные переменные:

| Переменная | Зачем нужна |
| --- | --- |
| `APP_ENV` | Режим приложения. В `local`, `dev`, `test` бот может показывать dev-детали. В `prod` скрывает их. |
| `MAX_BOT_TOKEN` | Секретный токен MAX-бота для исходящих запросов. |
| `MAX_BOT_USERNAME` | Ник бота без `@`; нужен для публичных ссылок на мероприятия. |
| `WEBHOOK_URL` | Публичный URL webhook. Нужен для регистрации подписки MAX и понимания текущего стенда. |
| `WEBHOOK_SECRET` | Секрет, который MAX присылает в заголовке `X-Max-Bot-Api-Secret`. |
| `WEBHOOK_PATH` | Путь FastAPI webhook, обычно `/webhook`. |
| `STORAGE_BACKEND` | `ydb` для YDB, `memory` только для тестов. |
| `YDB_ENDPOINT` | Адрес YDB: локально `grpc://localhost:2136`, в облаке `grpcs://ydb.serverless.yandexcloud.net:2135`. |
| `YDB_DATABASE` | Путь базы: локально `/local`, в Yandex Cloud полный путь вида `/ru-central1/.../...`. |
| `YDB_METADATA_CREDENTIALS` | `true` или `1` в Cloud Functions, чтобы брать IAM-токен из metadata service. |
| `SOURCE_DATABASE_URL` | Старый источник для миграции из PostgreSQL или SQLite. В обычной работе пусто. |
| `ADMIN_USER_IDS` | MAX user id администраторов через запятую. |
| `ORGANIZER_USER_IDS` | MAX user id Организаторов через запятую. Эти роли управляются конфигурацией. |
| `MAX_API_RPS` | Ограничение отправки уведомлений в MAX API: запросов в секунду. |
| `REMINDER_SYNC_INTERVAL_MINUTES` | Как часто запускать repair-синхронизацию напоминаний. |
| `REMINDER_SYNC_WINDOW_MINUTES` | В каком окне внутри интервала выполнять repair-синхронизацию. |
| `PERFORMANCE_METRICS_ENABLED` | Включает JSON-метрики `perf_metric`. |
| `PERFORMANCE_METRICS_SLOW_MS` | Порог, после которого метрика получает `slow=true`. |
| `DOCUMENTS_VERSION` | Версия текста согласия. При изменении юридического текста увеличивайте значение. |

Локальный пример:

```env
APP_ENV=local
MAX_BOT_TOKEN=replace_me
MAX_BOT_USERNAME=
WEBHOOK_URL=http://localhost:8080/webhook
WEBHOOK_SECRET=change_me_secret
WEBHOOK_PATH=/webhook
STORAGE_BACKEND=ydb
YDB_ENDPOINT=grpc://localhost:2136
YDB_DATABASE=/local
YDB_METADATA_CREDENTIALS=false
SOURCE_DATABASE_URL=
ADMIN_USER_IDS=
ORGANIZER_USER_IDS=
MAX_API_RPS=30
DOCUMENTS_VERSION=hackathon-2026-05
```

Продакшен-пример для Yandex Cloud:

```env
APP_ENV=prod
MAX_BOT_TOKEN=<секретный токен MAX>
MAX_BOT_USERNAME=<ник бота без @>
WEBHOOK_URL=https://functions.yandexcloud.net/<function_id>
WEBHOOK_SECRET=<секрет webhook>
WEBHOOK_PATH=/webhook
STORAGE_BACKEND=ydb
YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=/ru-central1/<cloud_id>/<database_id>
YDB_METADATA_CREDENTIALS=1
SOURCE_DATABASE_URL=
ADMIN_USER_IDS=<MAX user id администратора>
ORGANIZER_USER_IDS=<MAX user id Организаторов через запятую>
MAX_API_RPS=30
DOCUMENTS_VERSION=hackathon-2026-05
```

Не коммитьте `.env`. Секреты должны оставаться локально или в настройках версии Cloud Function.

## Выбор хостинга

У проекта есть два нормальных варианта размещения.

### Yandex serverless

Это текущий основной путь.

Используются:

- Yandex Cloud Functions для публичного HTTPS webhook;
- YDB Serverless для данных;
- timer trigger для уведомлений;
- service account для доступа функции к YDB.

Плюсы:

- не надо держать виртуальную машину;
- приложение просыпается по webhook и timer;
- удобно для демо, пилота и небольшой нагрузки;
- YDB и Cloud Functions находятся в одной облачной экосистеме.

Минусы:

- есть cold start: первый вызов после простоя может быть заметно медленнее;
- нужно понимать IAM-роли и service account;
- YDB отличается от привычных реляционных баз;
- стоимость зависит от фактических вызовов, чтений, записей и хранения.

Инструкция: [yandex-cloud.md](yandex-cloud.md).

### Свой сервер

Это обычный запуск FastAPI-приложения на VPS, выделенном сервере или любой платформе, где можно держать постоянно работающий процесс.

Минимальная схема:

```text
MAX -> HTTPS домен -> reverse proxy -> uvicorn app.main:app -> YDB или другая выбранная база
```

Reverse proxy — это фронтовой веб-сервер вроде Nginx или Caddy, который принимает HTTPS и прокидывает запросы в приложение на локальный порт.

Команда приложения:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Для уведомлений на своем сервере нужно отдельно запускать периодический вызов worker. В Cloud Functions это делает timer trigger, а на своем сервере эту роль может выполнять cron, systemd timer или отдельный процесс, который периодически вызывает тот же код `NotificationWorker`.

Плюсы:

- нет cold start;
- проще отлаживать долгоживущий процесс;
- можно держать постоянные фоновые задачи;
- больше контроля над сетью, логами и окружением.

Минусы:

- нужно обслуживать сервер, TLS, обновления и мониторинг;
- бесплатный режим обычно сложнее;
- для отказоустойчивости нужны дополнительные усилия;
- текущие штатные deploy-скрипты рассчитаны на Yandex Cloud Functions, а не на VPS.

Практически: если цель — быстрое демо или небольшой пилот, берите Yandex serverless. Если нужна постоянная низкая задержка, полный контроль и готовность администрировать сервер, выбирайте свой хостинг.

## Регистрация webhook в MAX

Для реальной работы нужен публичный HTTPS URL. Локальный `localhost` MAX не увидит.

Подписка MAX создается через `POST /subscriptions`. Официальная документация MAX указывает `https://platform-api.max.ru/subscriptions`, заголовок `Authorization` с токеном и HTTPS URL webhook.

Пример:

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

Для Yandex Cloud URL обычно выглядит так:

```text
https://functions.yandexcloud.net/<function_id>
```

Для своего сервера это будет ваш домен:

```text
https://bot.example.com/webhook
```

## Тесты и проверки

Полный набор тестов:

```bash
python -m pytest -q
```

Проверка только упаковки Cloud Functions:

```powershell
.\scripts\build-yc-package.ps1
```

```bash
bash scripts/build-yc-package.sh
```

Пакет создается в:

```text
dist/yc-package
dist/max-bot-yc.zip
```

Если менялись файлы приложения, перед деплоем в Yandex Cloud нужно пересобрать пакет и убедиться, что изменения попали в `dist/yc-package`.

## Где искать проблемы

Если `healthz` работает, а `readyz` нет — проблема в хранилище: endpoint, database path или credentials.

Если webhook возвращает `403` — не совпадает `WEBHOOK_SECRET`.

Если MAX не вызывает webhook — URL не публичный, не HTTPS или подписка создана не на тот адрес.

Если бот отвечает медленно — смотрите [performance-audit.md](performance-audit.md): там описаны `perf_metric`, cold start, YDB и MAX-вызовы.

Если не уходят уведомления — смотрите `notification_outbox`, timer trigger и логи вызовов timer.
