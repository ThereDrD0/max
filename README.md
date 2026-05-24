# MAX-бот записи абитуриента на мероприятие

Бот реализует сценарий из кейса: абитуриент принимает дисклеймер, выбирает мероприятие и слот, получает код записи, смотрит свои записи, отменяет их до начала мероприятия и управляет уведомлениями. Организатор видит свои мероприятия, ищет запись по коду, закрывает регистрацию, отмечает посещение и отправляет разрешённые уведомления.

Миниаппы не используются. Телефон, паспортные и банковские данные не собираются. Чувствительные настройки хранятся в `.env`; шаблон лежит в `.env.example`.

## Архитектура

Основной бесплатный вариант размещения: Yandex Cloud Functions + YDB Serverless. Docker оставлен для локальной разработки и тестов.

Локально приложение запускается как FastAPI-сервис:

- `POST /webhook` — входящие события MAX;
- `GET /healthz` — приложение живо;
- `GET /readyz` — приложение живо и хранилище доступно.

В облаке тот же обработчик используется через `index.handler`: `POST` принимает webhook MAX, `GET` отдаёт health, timer trigger отправляет уведомления из `notification_outbox`.

Подробная документация:

- [Работа с YDB](docs/ydb.md)
- [Работа с MAX API](docs/max-api.md)
- [Серверная часть в Yandex Cloud](docs/yandex-cloud.md)

## Быстрый локальный запуск через Docker

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

Docker Compose поднимет локальную YDB и бота. Бот создаст схему, загрузит `seed/events.yaml` и запустит `uvicorn` на порту `8080`.

Проверка:

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

## Локальная проверка webhook

Без доступа к MAX можно отправить тестовое событие:

```powershell
.\scripts\send-sample-update.ps1
```

```bash
bash scripts/send-sample-update.sh
```

Скрипт отправляет `bot_started` на `http://localhost:8080/webhook` с заголовком `X-Max-Bot-Api-Secret`.

## Локальный запуск без Docker

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

Если YDB запущена не на `localhost:2136`, поменяйте `YDB_ENDPOINT` и `YDB_DATABASE` в `.env`.

## ENV-настройки

Минимальный локальный пример:

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

Продакшен-пример:

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
ORGANIZER_USER_IDS=<MAX user id организаторов через запятую>
MAX_API_RPS=30
DOCUMENTS_VERSION=hackathon-2026-05
```

Подробная таблица переменных и нюансов находится в [документации по Yandex Cloud](docs/yandex-cloud.md#продакшен-env).

## Деплой в Yandex Cloud

Короткий путь:

```powershell
.\scripts\deploy-yc.ps1 -FunctionName max-bot -ServiceAccountId "<service_account_id>"
```

```bash
bash scripts/deploy-yc.sh max-bot "$SA_ID"
```

Deploy-скрипт сам собирает пакет и перед загрузкой новой версии применяет YDB-схему.

После деплоя проверьте:

```bash
curl https://functions.yandexcloud.net/<function_id>
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

Полный порядок создания YDB, service account, Cloud Function, публичного доступа, timer trigger и обновления продакшена описан в [документации по Yandex Cloud](docs/yandex-cloud.md).

## Миграция старой базы в YDB

Старый источник задаётся через `SOURCE_DATABASE_URL`, новая база — через `YDB_ENDPOINT` и `YDB_DATABASE`.

```bash
export SOURCE_DATABASE_URL='postgresql+psycopg://user:password@host:5432/maxbot'
export STORAGE_BACKEND=ydb
export YDB_ENDPOINT='grpcs://ydb.serverless.yandexcloud.net:2135'
export YDB_DATABASE='/ru-central1/<cloud_id>/<database_id>'
export YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
python -m app.ydb_schema
python -m app.migration
```

Подробности переноса и проверки данных описаны в [документации по YDB](docs/ydb.md#миграция-из-postgresql-или-sqlite).

## Тесты

Локально:

```bash
python -m pytest -q
```

В Docker:

```bash
docker compose run --rm bot python -m pytest -q
```

Проверка пакета Cloud Functions:

```powershell
.\scripts\build-yc-package.ps1
```

```bash
bash scripts/build-yc-package.sh
```

## Что важно помнить

MAX не сможет вызвать локальный `localhost`; для настоящего webhook нужен публичный HTTPS URL. В продакшене сейчас используется прямой URL Cloud Functions без API Gateway.

Ссылки на конкретные мероприятия строятся как `https://max.ru/<botName>?start=e_<event-slug>`. В карточке бот показывает строку `Ссылка: Нажмите чтобы скопировать` и кнопку `Поделиться`, которая копирует ссылку в буфер обмена. Для старых мероприятий без slug бот создаёт slug при первом показе карточки. `MAX_BOT_USERNAME` лучше заполнить явно, но если он пустой, бот попробует получить ник через MAX API `/me`. В подписке MAX оставьте update-тип `bot_started`.

Serverless не означает “бесконечно бесплатно”. Для демо и пилота выбран вариант без VM, Container Registry и API Gateway, но расходы всё равно возможны при превышении лимитов Yandex Cloud. Включите бюджетные уведомления в Billing.
