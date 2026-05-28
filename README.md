# MAX-бот записи на мероприятия университета

Это бот для MAX, который помогает абитуриентам записываться на мероприятия университета, а Организаторам управлять этими мероприятиями и участниками.

Проект можно запустить локально, поднять на своем сервере как обычное FastAPI-приложение или развернуть в Yandex Cloud Functions вместе с YDB Serverless. Основной продакшен-сценарий сейчас рассчитан на Yandex serverless: webhook MAX вызывает Cloud Function, данные лежат в YDB, а напоминания отправляются по timer trigger.

## Что умеет бот

Абитуриент:

- принимает дисклеймер о минимальной обработке данных;
- смотрит каталог ближайших мероприятий;
- открывает карточку мероприятия по меню или публичной ссылке;
- выбирает слот, если мероприятие разделено на временные окна;
- получает код записи;
- смотрит свои записи, отменяет их и включает или отключает уведомления.

Организатор:

- видит доступные ему мероприятия;
- создает и редактирует мероприятия;
- закрывает регистрацию или все мероприятие;
- смотрит участников;
- ищет запись по коду;
- отмечает посещение;
- отправляет разрешенные уведомления участникам.

Администратор:

- назначает и снимает роль Организатора;
- видит Организаторов, которые назначены через интерфейс и через конфигурацию.

Миниаппы не используются. Телефон, паспортные и банковские данные не собираются. Бот хранит только MAX user id, отображаемое имя, выбранное мероприятие, запись, статус записи и служебные данные для уведомлений.

## Карта документации

Начните отсюда, если впервые видите кодовую базу:

- [Архитектура проекта](docs/architecture.md) — как связаны webhook, обработчики, сервисы, хранилище, YDB и уведомления.
- [Пользовательские сценарии](docs/user-flows.md) — как бот ведет абитуриента, Организатора и администратора.
- [Запуск проекта с нуля](docs/development.md) — локальный запуск, переменные окружения и выбор хостинга: свой сервер или Yandex serverless.
- [Работа с MAX API](docs/max-api.md) — webhook, подписки, кнопки, диплинки, отправка сообщений и картинки.
- [Работа с YDB](docs/ydb.md) — схема данных, транзакции, индексы и сравнение с PostgreSQL.
- [Деплой в Yandex Cloud](docs/yandex-cloud.md) — Cloud Functions, YDB Serverless, service account, timer trigger, сборка и обновление версии.
- [Производительность и метрики](docs/performance-audit.md) — `perf_metric`, cold start, YDB-вызовы, MAX-вызовы и диагностика задержек.
- [Глоссарий UI](docs/glossary.md) — принятые пользовательские термины.
- [Картинки MAX](docs/max-image-assets.md) — как заранее загрузить часто используемые картинки и отправлять их по `token`.

## Быстрый локальный запуск

На Windows:

```powershell
Copy-Item .env.example .env
notepad .env
.\scripts\dev-up.ps1 -Build
```

На Linux:

```bash
cp .env.example .env
nano .env
bash scripts/dev-up.sh --build
```

Docker Compose поднимет локальную YDB и приложение на порту `8080`. Проверка:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
```

Локальная YDB UI:

```text
http://localhost:8765
```

MAX не сможет вызвать ваш `localhost` напрямую. Для настоящего webhook нужен публичный HTTPS-адрес: Cloud Functions, свой сервер с доменом и TLS или временный tunnel-сервис для разработки.

## Локальная проверка webhook

Без доступа к MAX можно отправить тестовое событие:

```powershell
.\scripts\send-sample-update.ps1
```

```bash
bash scripts/send-sample-update.sh
```

Скрипт отправляет `bot_started` на локальный endpoint `POST /webhook` с заголовком `X-Max-Bot-Api-Secret`.

## ENV минимум

Шаблон лежит в [.env.example](.env.example). Для локального Docker-запуска достаточно заполнить:

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
ADMIN_USER_IDS=
ORGANIZER_USER_IDS=
MAX_API_RPS=30
DOCUMENTS_VERSION=hackathon-2026-05
```

Подробно все переменные разобраны в [документации по запуску](docs/development.md#env-настройки).

## Тесты

Локально:

```bash
python -m pytest -q
```

В Docker:

```bash
docker compose run --rm bot python -m pytest -q
```

## Деплой

Короткая команда для Yandex Cloud Functions:

```powershell
.\scripts\deploy-yc.ps1 -FunctionName max-bot -ServiceAccountId "<service_account_id>"
```

```bash
bash scripts/deploy-yc.sh max-bot "$SA_ID"
```

Скрипт собирает пакет, применяет YDB-схему и создает новую версию Cloud Function. Полный порядок настройки облака описан в [docs/yandex-cloud.md](docs/yandex-cloud.md).

Если вы не хотите Yandex serverless, проект можно запустить на своем сервере как обычный FastAPI-сервис через Docker Compose или `uvicorn`. Этот вариант описан в [docs/development.md](docs/development.md#вариант-2-свой-сервер).
