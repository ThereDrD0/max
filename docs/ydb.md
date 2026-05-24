# Работа с YDB

Этот проект использует YDB как основное хранилище для продакшена и локальной разработки. В рабочем коде нет SQLAlchemy и Alembic: приложение обращается к YDB напрямую через официальный Python SDK `ydb`. SQLAlchemy оставлен только в optional-зависимостях для миграции старой PostgreSQL/SQLite-базы.

## Почему YDB

YDB Serverless хорошо подходит этому боту, потому что нагрузка событийная: webhook получает короткое событие от MAX, быстро читает/пишет несколько строк и отдаёт `200`. Нет постоянно работающего сервера базы на виртуальной машине, нет ручного обслуживания PostgreSQL, а Cloud Functions может подключаться к YDB через сервисный аккаунт.

Главное отличие от PostgreSQL для нас: мы не полагаемся на автоинкремент, внешние ключи и Alembic-миграции. Идентификаторы генерирует приложение, связи проверяются сервисным слоем, а схема создаётся идемпотентным YQL из `app.ydb_schema`. Это чуть менее привычно, зато проще для serverless-развёртывания и бесплатного стенда.

## Где YDB подключается в коде

Основная фабрика хранилища находится в `app/storage/factory.py`. Значение `STORAGE_BACKEND=ydb` создаёт `YdbStorage`, который получает:

```env
YDB_ENDPOINT=...
YDB_DATABASE=...
YDB_METADATA_CREDENTIALS=...
```

`YdbStorage` использует `ydb.Driver` и `ydb.QuerySessionPool`. Обычные запросы идут через `execute_with_retries`, а запись на мероприятие и отмена выполняются в serializable read-write транзакциях. Serializable read-write транзакция означает, что YDB защищает последовательность “проверить места -> создать запись -> увеличить счётчик” от гонок при одновременной записи нескольких пользователей.

Локальный FastAPI и облачная функция используют один и тот же слой хранения. Разница только в переменных окружения и способе получения прав доступа.

## Локальная YDB

В Docker Compose поднимается контейнер:

```yaml
ydbplatform/local-ydb:latest
```

Он доступен:

```text
grpc://localhost:2136
```

из хоста и:

```text
grpc://ydb:2136
```

из контейнера `bot`.

Локальная база:

```text
/local
```

Веб-интерфейс локальной YDB доступен по адресу:

```text
http://localhost:8765
```

Типичный `.env` для локального запуска на хосте:

```env
APP_ENV=local
STORAGE_BACKEND=ydb
YDB_ENDPOINT=grpc://localhost:2136
YDB_DATABASE=/local
YDB_METADATA_CREDENTIALS=false
```

В Docker Compose `YDB_ENDPOINT` переопределяется на `grpc://ydb:2136`, поэтому руками менять его для контейнерного запуска не нужно.

Команды:

```powershell
Copy-Item .env.example .env
.\scripts\dev-up.ps1 -Build
```

```bash
cp .env.example .env
bash scripts/dev-up.sh --build
```

При старте контейнер бота делает три вещи:

1. ждёт доступности YDB;
2. запускает `python -m app.ydb_schema`;
3. запускает `python -m app.seed`;
4. стартует `uvicorn app.main:app --host 0.0.0.0 --port 8080`.

Проверка:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
```

`healthz` отвечает, что приложение живо. `readyz` дополнительно проверяет, что хранилище отвечает.

## YDB Serverless в Yandex Cloud

В облаке используется serverless-база. Для текущего стенда значения такие:

```text
YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=/ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt
```

`grpcs` означает gRPC поверх TLS. В local-ydb используется `grpc` без TLS.

Для Cloud Functions нужно:

```env
YDB_METADATA_CREDENTIALS=1
```

Это говорит SDK брать IAM-токен из metadata service функции. Metadata service — это внутренний сервис Yandex Cloud, через который функция получает временные права своего service account. Поэтому в продакшен `.env` не нужен файл ключа сервисного аккаунта и не нужен пользовательский OAuth-токен.

Сервисному аккаунту функции нужна роль на конкретную YDB-базу. Для этого проекта используется `ydb.editor`, потому что бот создаёт схему, пишет записи, обновляет счётчики, меняет статусы и пишет очередь уведомлений. Более строгую роль можно проектировать отдельно, но для хакатонного стенда `ydb.editor` на конкретную базу проще и понятнее, чем широкая роль на весь каталог.

## Права доступа и варианты авторизации

SDK выбирает credentials так:

1. Если задан `YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS` или `YDB_ACCESS_TOKEN_CREDENTIALS`, используются credentials из переменных окружения.
2. Если `YDB_METADATA_CREDENTIALS=1`, SDK берёт токен из metadata service.
3. Иначе используется `AnonymousCredentials`, что подходит для local-ydb.

Практически это даёт три режима.

Локальный Docker:

```env
YDB_ENDPOINT=grpc://localhost:2136
YDB_DATABASE=/local
YDB_METADATA_CREDENTIALS=false
```

Локальная работа с облачной YDB через ваш `yc`-профиль:

```powershell
$yc = Join-Path $env:USERPROFILE "yandex-cloud\bin\yc.exe"
$env:YDB_ENDPOINT = "grpcs://ydb.serverless.yandexcloud.net:2135"
$env:YDB_DATABASE = "/ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt"
$env:YDB_ACCESS_TOKEN_CREDENTIALS = & $yc iam create-token
python -m app.ydb_schema
Remove-Item Env:\YDB_ACCESS_TOKEN_CREDENTIALS
```

То же в Linux:

```bash
export YDB_ENDPOINT='grpcs://ydb.serverless.yandexcloud.net:2135'
export YDB_DATABASE='/ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt'
export YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
python -m app.ydb_schema
unset YDB_ACCESS_TOKEN_CREDENTIALS
```

Cloud Functions:

```env
YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=/ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt
YDB_METADATA_CREDENTIALS=1
```

Нюанс: `YDB_ACCESS_TOKEN_CREDENTIALS` — временный токен. Его удобно использовать для локального администрирования, но не надо зашивать в `.env` продакшена.

## Схема данных

Схема задаётся в `app/ydb_schema.py`. Команда:

```bash
python -m app.ydb_schema
```

создаёт таблицы через `CREATE TABLE IF NOT EXISTS`, поэтому её можно запускать повторно. Это не полноценная система миграций с изменением колонок, но для текущей схемы и демо-развёртывания достаточно. Если в будущем нужно будет менять существующие колонки или индексы, лучше добавить явные versioned migration-скрипты.

Таблицы:

| Таблица | Назначение |
| --- | --- |
| `users` | Пользователи MAX: `user_id`, отображаемое имя, признак бота, даты создания/обновления. |
| `consents` | Согласия пользователя на обработку минимальных данных профиля. Хранит версию документа. |
| `bot_sessions` | Последнее сообщение бота для пользователя. Нужно, чтобы бот удалял или редактировал старое меню и не засорял чат. |
| `events` | Мероприятия: название, описание, требования, дата, формат, место/ссылка, вместимость, флаг закрытой регистрации, счётчик занятых мест. |
| `event_slots` | Слоты мероприятия. Используются, когда одно мероприятие делится на несколько временных окон. |
| `event_deeplinks` | Стабильные slug для публичных ссылок на мероприятия. |
| `event_images` | Обложки мероприятий: MAX `token` и запасной `url`, без хранения бинарных файлов у нас. |
| `pending_event_images` | Совместимость со старым сценарием ожидания картинки для мероприятия. |
| `organizer_states` | Текущее состояние диалога организатора: создание, пересборка, изменение даты, времени или места. |
| `role_assignments` | Роли пользователей: `admin`, `organizer`. |
| `organizer_events` | Связь организатора с мероприятиями, которые он может видеть и администрировать. |
| `registrations` | Записи пользователей на мероприятия: код, статус, слот, флаг уведомлений, даты отмены/посещения. |
| `registration_codes` | Служебная таблица уникальности кодов записи. |
| `active_registration_keys` | Служебная таблица уникальности активной записи пользователя на мероприятие. |
| `notification_outbox` | Очередь уведомлений: напоминания за сутки/час и ручные уведомления организатора. |
| `audit_log` | Аудит действий: согласие, создание/отмена записи, закрытие регистрации, уведомления и смена статусов. |

Важный нюанс: YDB-таблицы здесь не содержат внешних ключей. Например, `registrations.event_id` ссылается на `events.id` по смыслу, но БД это не проверяет. Проверки выполняет сервисный слой. Поэтому любые будущие обходные импорты должны соблюдать те же связи.

## Индексы

В схеме есть глобальные вторичные индексы:

| Индекс | Зачем нужен |
| --- | --- |
| `idx_consents_user` | Быстро проверить, дал ли пользователь согласие. |
| `idx_events_starts_at` | Сортировка и фильтрация мероприятий по времени. |
| `idx_slots_event` | Получить слоты мероприятия. |
| `idx_event_deeplinks_event` | Найти публичный slug по id мероприятия. |
| `idx_roles_user` | Проверить роль пользователя. |
| `idx_organizer_events_user` | Получить мероприятия организатора. |
| `idx_organizer_events_event` | Найти организаторов события при будущих расширениях. |
| `idx_registrations_code` | Поиск записи по коду. |
| `idx_registrations_user` | Список записей пользователя. |
| `idx_registrations_event` | Список записей на мероприятие для организатора. |
| `idx_outbox_status_send` | Быстро выбрать уведомления, которые пора отправить. |
| `idx_outbox_registration` | Связать уведомления с записью. |
| `idx_audit_entity` | Смотреть аудит по сущности. |

## Идентификаторы

Автонумерации в YDB-схеме нет. Приложение генерирует `Int64` через `secrets.randbits(62)` и проверяет отсутствие такого id в таблице. Это происходит в `YdbStorage._new_id`.

Почему так:

- не нужен отдельный sequence-сервис;
- схема проще переносится между local-ydb и serverless YDB;
- вероятность коллизии мала, а проверка на существование всё равно есть.

Минус: id выглядят длинными. Поэтому в пользовательском интерфейсе они скрыты в `prod`-режиме, а в dev-режиме показываются только с префиксом `[DEV]`.

## Запись на мероприятие и конкурентность

Самая чувствительная операция — запись на последнее место. Она выполняется в YDB-транзакции:

1. Проверяется согласие пользователя.
2. Читается мероприятие и слоты.
3. Проверяется, что регистрация не закрыта и мероприятие ещё не началось.
4. Проверяется, что у пользователя нет активной записи на то же мероприятие.
5. Проверяется свободное место.
6. Создаётся запись.
7. Создаётся уникальный код в `registration_codes`.
8. Создаётся активный ключ в `active_registration_keys`.
9. Увеличивается `booked_count`.
10. Создаются напоминания в `notification_outbox`.
11. Пишется аудит.

Счётчики `booked_count` лежат в `events` и `event_slots`. Это сделано намеренно: считать занятые места каждый раз через полный список записей дороже и сложнее при росте количества пользователей.

Если запись отменяется до начала мероприятия, `booked_count` уменьшается и активный ключ удаляется. Если поздняя отмена разрешена политикой мероприятия, статус становится `late_canceled`; если запрещена, сервис отдаёт ошибку.

## Seed-данные

Стартовые мероприятия лежат в:

```text
seed/events.yaml
```

Загрузка:

```bash
python -m app.seed
```

Seed работает идемпотентно по паре `(title, starts_at)`: если мероприятие с таким названием и временем уже есть, оно не создаётся повторно. Поле `slug` назначает стабильный публичный идентификатор для ссылки вида `https://max.ru/<botName>?start=e_<slug>`.

Организаторы назначаются двумя способами:

- через `ORGANIZER_USER_IDS` из `.env`: эти пользователи получают доступ ко всем seed-мероприятиям;
- через поле `organizer_user_ids` в конкретном мероприятии seed-файла.

Администраторы задаются через `ADMIN_USER_IDS` и синхронизируются при bootstrap.

## Миграция из PostgreSQL или SQLite

Миграция запускается так:

```bash
python -m app.migration
```

Перед этим нужно задать:

```env
SOURCE_DATABASE_URL=...
STORAGE_BACKEND=ydb
YDB_ENDPOINT=...
YDB_DATABASE=...
```

Пример PostgreSQL:

```bash
export SOURCE_DATABASE_URL='postgresql+psycopg://user:password@host:5432/maxbot'
export STORAGE_BACKEND=ydb
export YDB_ENDPOINT='grpcs://ydb.serverless.yandexcloud.net:2135'
export YDB_DATABASE='/ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt'
export YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
python -m app.ydb_schema
python -m app.migration
```

Пример SQLite:

```bash
export SOURCE_DATABASE_URL='sqlite:///./data/local.db'
python -m app.migration
```

Миграция делает snapshot старой базы, импортирует его в текущее хранилище и проверяет базовые количества:

- пользователи;
- мероприятия;
- записи;
- уведомления.

При импорте пересоздаются служебные таблицы `registration_codes` и `active_registration_keys`, а `booked_count` пересчитывается по активным записям. Это важно: нельзя просто перенести старые счётчики, если в старой базе они могли расходиться с фактическими регистрациями.

## Проверка данных

Локально откройте:

```text
http://localhost:8765
```

В Yandex Cloud откройте Managed Service for YDB -> нужная база -> Navigation. Там можно смотреть таблицы, количество строк и выполнять YQL-запросы.

Примеры запросов:

```sql
SELECT * FROM events ORDER BY starts_at;
```

```sql
SELECT code, user_id, event_id, status
FROM registrations
ORDER BY created_at DESC
LIMIT 20;
```

```sql
SELECT *
FROM notification_outbox
WHERE status = "pending"
ORDER BY send_after;
```

```sql
SELECT *
FROM bot_sessions;
```

Если проверяете через CLI, сначала убедитесь, что CLI настроен на нужный cloud/folder, а credentials имеют доступ к базе.

## Типичные ошибки

`Unauthenticated` или `Access denied`.

Причина почти всегда в credentials. Локально для облачной YDB нужен `YDB_ACCESS_TOKEN_CREDENTIALS`, в Cloud Functions нужен `YDB_METADATA_CREDENTIALS=1` и роль сервисного аккаунта на базе.

`Endpoint is unavailable`.

Проверьте протокол и порт. Для облака нужен `grpcs://...:2135`, для local-ydb обычно `grpc://localhost:2136`.

`Database not found`.

Проверьте полный `YDB_DATABASE`. Для Yandex Cloud это не имя базы `ydb563`, а путь вида `/ru-central1/.../...`.

`readyz` отдаёт 503.

Приложение живо, но база недоступна. Проверьте контейнер YDB, endpoint, database path и credentials.

Повторные seed-мероприятия не появляются.

Seed не создаёт дубликаты по `(title, starts_at)`. Если нужно создать похожее мероприятие, измените дату/время или название.

Старые сообщения бота не удаляются.

Удаление работает только для сообщений, id которых был сохранён в `bot_sessions`. Сообщения, отправленные до добавления этого механизма, могут остаться в чате.

## Бесплатный режим и осторожность

Serverless не значит “бесконечно бесплатно”. Сейчас архитектура выбрана так, чтобы не держать постоянно работающие серверы и не использовать Container Registry/API Gateway. Но если вы превысите бесплатные лимиты по вызовам функций, хранению, чтениям или записям, Yandex Cloud начнёт тарифицировать потребление.

Минимальные меры:

- держать `sls-provisioned-rcu` равным `0`;
- не хранить большие файлы в YDB;
- не писать персональные данные сверх требуемого минимума;
- включить бюджетные уведомления в Billing;
- периодически смотреть графики YDB и Cloud Functions;
- не запускать бесконечные циклы или частые тестовые webhook-спамы в продакшене.
