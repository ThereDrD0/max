# Работа с YDB

YDB — основное хранилище проекта для локальной разработки и продакшена. Приложение обращается к ней напрямую через официальный Python SDK `ydb`, без SQLAlchemy и без ORM.

SQLAlchemy и драйверы PostgreSQL оставлены только в optional-зависимостях для миграции старой базы. В обычной работе они не участвуют.

## Почему здесь YDB

Бот работает событийно: пришел webhook от MAX, приложение быстро прочитало и записало несколько строк, ответило `200`, а потом заснуло до следующего события. Для такой нагрузки YDB Serverless хорошо сочетается с Yandex Cloud Functions:

- не нужен постоянно работающий сервер базы;
- Cloud Functions может получать права к YDB через service account;
- YDB сама масштабирует serverless-нагрузку в пределах лимитов;
- локальная разработка возможна через `ydbplatform/local-ydb`.

Цена этого выбора: YDB не ведет себя как привычный PostgreSQL. Нужно явно думать о первичных ключах, вторичных индексах, количестве сетевых запросов и отсутствии привычных внешних ключей.

## Сравнение с PostgreSQL

PostgreSQL — популярная западная реляционная база, поэтому сравнивать удобнее с ней.

| Тема | PostgreSQL | YDB в этом проекте |
| --- | --- | --- |
| Подключение | Обычно один сервер или managed-кластер, постоянные соединения, SQL-драйвер. | Serverless endpoint, gRPC, Python SDK `ydb.Driver` и `QuerySessionPool`. |
| Схема | Часто миграции через Alembic, `SERIAL`/`IDENTITY`, внешние ключи. | `app.ydb_schema` с `CREATE TABLE IF NOT EXISTS`; id генерирует приложение; внешних ключей нет. |
| ORM | Часто SQLAlchemy или Django ORM. | ORM нет, запросы и маппинг явные. |
| Транзакции | Привычные SQL-транзакции, `SELECT ... FOR UPDATE`, constraints. | Serializable read-write транзакции YDB для чувствительных операций. |
| Индексы | Оптимизатор часто сам выбирает индекс. | Для вторичного индекса в YQL нужно явно писать `VIEW idx_name`. |
| Автоинкремент | Типичный путь для id. | Приложение генерирует `Int64` через `secrets.randbits(62)` и проверяет коллизию. |
| Внешние ключи | База может проверять связи. | Связи проверяются сервисным слоем и транзакциями. |
| Стоимость запроса | Часто думают о CPU/IO сервера. | В serverless особенно важны round-trip, RCU/WCU и количество запросов. |

Главный практический вывод: в PostgreSQL легко написать красивый ORM-код и надеяться на оптимизатор. В YDB в этом проекте лучше писать запросы под конкретный экран или операцию, не делать лишних чтений и осознанно использовать индексы.

## Где YDB подключается

Фабрика хранилища:

```text
app/storage/factory.py
```

При `STORAGE_BACKEND=ydb` создается `YdbStorage` из:

```text
app/storage/ydb.py
```

Настройки:

```env
STORAGE_BACKEND=ydb
YDB_ENDPOINT=...
YDB_DATABASE=...
YDB_METADATA_CREDENTIALS=...
```

`YdbStorage` использует:

- `ydb.Driver` — подключение к базе;
- `ydb.QuerySessionPool` — пул сессий для YQL-запросов;
- retry-обертки для повторов при временных ошибках;
- read-write транзакции для записи на мероприятие и отмены записи.

## Режимы авторизации

Локальная YDB:

```env
YDB_ENDPOINT=grpc://localhost:2136
YDB_DATABASE=/local
YDB_METADATA_CREDENTIALS=false
```

В этом режиме используются anonymous credentials. Это нормально для `local-ydb`.

Локальная работа с облачной YDB:

```powershell
$yc = Join-Path $env:USERPROFILE "yandex-cloud\bin\yc.exe"
$env:YDB_ENDPOINT = "grpcs://ydb.serverless.yandexcloud.net:2135"
$env:YDB_DATABASE = "/ru-central1/<cloud_id>/<database_id>"
$env:YDB_ACCESS_TOKEN_CREDENTIALS = & $yc iam create-token
python -m app.ydb_schema
Remove-Item Env:\YDB_ACCESS_TOKEN_CREDENTIALS
```

Cloud Functions:

```env
YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=/ru-central1/<cloud_id>/<database_id>
YDB_METADATA_CREDENTIALS=1
```

`YDB_METADATA_CREDENTIALS=1` означает: функция берет временный IAM-токен из metadata service Yandex Cloud. Metadata service — внутренний сервис облака, который выдает функции credentials ее service account.

Не храните `YDB_ACCESS_TOKEN_CREDENTIALS` в продакшен `.env`: это временный токен для локального администрирования.

## Схема

Схема задается в:

```text
app/ydb_schema.py
```

Применить схему:

```bash
python -m app.ydb_schema
```

Скрипт создает таблицы через `CREATE TABLE IF NOT EXISTS`. Его можно запускать повторно. Но это не полноценная versioned migration-система. Если в будущем нужно менять существующие колонки, удалять индексы или переносить данные между версиями схемы, лучше добавить отдельные миграционные скрипты с явными версиями.

## Таблицы

| Таблица | Назначение |
| --- | --- |
| `users` | Пользователи MAX: id, имя, признак бота, даты создания и обновления. |
| `consents` | Согласия на обработку минимальных данных профиля. |
| `events` | Мероприятия: название, описание, требования, дата, формат, место, вместимость, счетчик занятых мест. |
| `event_slots` | Слоты мероприятия, если одно мероприятие делится на временные окна. |
| `event_deeplinks` | Стабильные slug для публичных ссылок на мероприятия. |
| `event_images` | Обложки мероприятий: MAX `token` или запасной `url`. |
| `pending_event_images` | Служебное ожидание картинки при редактировании мероприятия. |
| `organizer_states` | Черновики диалогов Организатора. |
| `role_assignments` | Роли `admin` и `organizer`. |
| `organizer_events` | Связь Организатора с мероприятиями. |
| `registrations` | Записи пользователей на мероприятия. |
| `registration_codes` | Уникальность кодов записи. |
| `active_registration_keys` | Уникальность активной записи пользователя на мероприятие. |
| `notification_outbox` | Очередь уведомлений. |
| `audit_log` | Аудит важных действий. |

В YDB-схеме проекта нет внешних ключей. Например, `registrations.event_id` ссылается на `events.id` по смыслу, но база это не проверяет. Проверки выполняются в сервисах и транзакциях. Поэтому любые импорты и ручные правки должны сохранять связи самостоятельно.

## Индексы

Вторичные индексы создаются под конкретные запросы приложения:

| Индекс | Зачем нужен |
| --- | --- |
| `idx_consents_user` | Проверить согласие пользователя. |
| `idx_events_starts_at` | Выбрать будущие мероприятия по времени. |
| `idx_slots_event` | Получить слоты мероприятия. |
| `idx_event_deeplinks_event` | Найти slug по id мероприятия. |
| `idx_roles_user` | Получить роли пользователя. |
| `idx_organizer_events_user` | Найти мероприятия Организатора. |
| `idx_organizer_events_event` | Найти Организаторов мероприятия. |
| `idx_registrations_code` | Найти запись по коду. |
| `idx_registrations_user` | Список записей пользователя. |
| `idx_registrations_event` | Список записей на мероприятие. |
| `idx_outbox_status_send` | Выбрать уведомления, которые пора отправить. |
| `idx_outbox_registration` | Найти уведомления по записи. |
| `idx_audit_entity` | Смотреть аудит по сущности. |

Особенность YDB: при чтении через вторичный индекс его нужно явно указать в `VIEW`.

Пример:

```sql
SELECT *
FROM registrations VIEW idx_registrations_user
WHERE user_id = $user_id;
```

Это соответствует официальной документации YDB по secondary indexes: для доступа через вторичный индекс имя индекса указывается в `VIEW`. Документация: [ydb.tech/docs/en/dev/secondary-indexes](https://ydb.tech/docs/en/dev/secondary-indexes).

## Идентификаторы

В проекте нет автоинкремента. `YdbStorage._new_id` генерирует `Int64` через `secrets.randbits(62)` и проверяет, что такого id еще нет.

Почему так:

- не нужен отдельный sequence-сервис;
- одинаково работает в local-ydb и serverless YDB;
- id генерируется приложением до записи;
- вероятность коллизии мала, а проверка все равно есть.

Минус: id длинные и не подходят как пользовательские коды. Поэтому пользователю показывается короткий код записи, а не внутренний id.

## Запись на мероприятие

Самая чувствительная операция — создать запись на ограниченное число мест. Она должна быть атомарной.

Логика:

1. Проверить согласие пользователя.
2. Прочитать мероприятие и слот.
3. Проверить, что регистрация открыта.
4. Проверить, что мероприятие еще не началось.
5. Проверить, что у пользователя нет активной записи на то же мероприятие.
6. Проверить свободное место.
7. Создать запись.
8. Создать уникальный код в `registration_codes`.
9. Создать активный ключ в `active_registration_keys`.
10. Увеличить `booked_count`.
11. Создать напоминания в `notification_outbox`.
12. Записать аудит.

Это выполняется внутри serializable read-write транзакции. Serializable — самый строгий уровень изоляции: параллельные операции выглядят так, будто они выполнены последовательно. Нам это нужно, чтобы два пользователя не заняли одно последнее место.

## Счетчики мест

Свободные места считаются через сохраненные счетчики:

- `events.booked_count` для мероприятия без слотов;
- `event_slots.booked_count` для конкретного слота.

Это намеренная денормализация. Денормализация — когда часть данных хранится повторно ради скорости чтения. Можно было бы каждый раз считать активные записи через `registrations`, но каталог и карточки мероприятия открываются часто, поэтому счетчик дешевле.

Важно: все изменения записи должны поддерживать счетчик. Отмена записи уменьшает счетчик, создание увеличивает, закрытие мероприятия меняет статусы активных записей.

## Seed-данные

Стартовые мероприятия:

```text
seed/events.yaml
```

Загрузка:

```bash
python -m app.seed
```

Seed идемпотентен по паре `(title, starts_at)`: если такое мероприятие уже есть, повторно оно не создается.

Организаторы назначаются:

- через `ORGANIZER_USER_IDS` — получают доступ ко всем seed-мероприятиям;
- через `organizer_user_ids` у конкретного мероприятия в YAML.

Администраторы задаются через `ADMIN_USER_IDS`.

## Миграция из PostgreSQL или SQLite

Миграция нужна только если есть старая база. Она запускается так:

```bash
python -m app.migration
```

Перед запуском:

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
export YDB_DATABASE='/ru-central1/<cloud_id>/<database_id>'
export YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
python -m app.ydb_schema
python -m app.migration
unset YDB_ACCESS_TOKEN_CREDENTIALS
```

Миграция делает snapshot старой базы, импортирует его в текущее хранилище и пересоздает служебные таблицы уникальности: `registration_codes` и `active_registration_keys`.

## Проверка данных

Локально:

```text
http://localhost:8765
```

В Yandex Cloud Console откройте Managed Service for YDB, выберите базу и перейдите в Navigation.

Полезные YQL-запросы:

```sql
SELECT *
FROM events
ORDER BY starts_at;
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

## Производительность YDB

В serverless-архитектуре важна не только длительность одного запроса, но и количество запросов подряд. Один экран бота не должен раскрывать простой сценарий в десятки round-trip к базе.

Round-trip — это полный сетевой поход приложения в базу и обратно. Даже если каждый запрос быстрый, много последовательных round-trip дают заметную задержку.

Практические правила:

- не используйте универсальный “богатый” `get_event` там, где нужен только заголовок;
- загружайте связанные данные пачками, если экран показывает список;
- используйте `VIEW idx_name` для запросов через вторичные индексы;
- не читайте пользователя перед каждым `UPSERT`, если достаточно `touch_user`;
- смотрите `ydb_calls`, `ydb_ms` и `ydb_methods` в `perf_metric`.

Подробная диагностика описана в [performance-audit.md](performance-audit.md).

## Типичные ошибки

`Unauthenticated` или `Access denied`.

Почти всегда проблема в credentials. Локально для облачной YDB нужен `YDB_ACCESS_TOKEN_CREDENTIALS`; в Cloud Functions нужен `YDB_METADATA_CREDENTIALS=1` и роль service account на базе.

`Endpoint is unavailable`.

Проверьте протокол и порт. Для облака нужен `grpcs://...:2135`, для local-ydb обычно `grpc://localhost:2136`.

`Database not found`.

Проверьте полный `YDB_DATABASE`. В Yandex Cloud это путь вида `/ru-central1/.../...`, а не короткое имя базы.

`readyz` возвращает 503.

Приложение живо, но хранилище недоступно. Проверяйте контейнер YDB, endpoint, database path и credentials.

Повторные seed-мероприятия не появляются.

Seed не создает дубликаты по `(title, starts_at)`. Чтобы создать похожее мероприятие, поменяйте название или дату.

## Ссылки

- [YDB secondary indexes](https://ydb.tech/docs/en/dev/secondary-indexes)
- [YDB документация](https://ydb.tech/docs)
