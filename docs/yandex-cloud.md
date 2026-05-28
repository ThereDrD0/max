# Деплой в Yandex Cloud serverless

Этот документ описывает продакшен-размещение проекта в Yandex Cloud без собственного сервера. Основная схема:

```text
MAX webhook
  -> Yandex Cloud Functions
  -> приложение Python
  -> YDB Serverless

Timer trigger
  -> та же Cloud Function
  -> NotificationWorker
  -> MAX API
```

Cloud Functions принимает webhook и timer-события. YDB Serverless хранит данные. Service account дает функции право работать с YDB.

## Что используется

Нужные сервисы:

- Cloud Functions — публичный HTTPS endpoint для webhook MAX;
- YDB Serverless — база данных;
- Service account — техническая учетная запись функции;
- Timer trigger — периодический запуск отправки уведомлений;
- Cloud Logging — логи функции.

Сознательно не обязательны:

- API Gateway — прямого URL Cloud Functions достаточно;
- Container Registry — функция деплоится исходниками Python;
- VM/VPS — нет постоянно работающего сервера.

## Что нужно заранее

На локальной машине:

- настроенный `yc`;
- доступ к нужному cloud и folder;
- `.env` с продакшен-переменными;
- `MAX_BOT_TOKEN`;
- `WEBHOOK_SECRET`;
- id service account.

Проверить `yc`:

```bash
yc config list
```

На Windows, если `yc` не в `PATH`, он может лежать здесь:

```powershell
& "$env:USERPROFILE\yandex-cloud\bin\yc.exe" config list
```

Скрипт `scripts/deploy-yc.ps1` сначала ищет `yc` в `PATH`, потом пробует этот путь.

## Создание YDB Serverless

Создать базу:

```bash
yc ydb database create max-bot-ydb \
  --serverless \
  --sls-storage-size 1GB \
  --sls-throttling-rcu 10 \
  --sls-provisioned-rcu 0
```

Получить endpoint и database path:

```bash
yc ydb database get max-bot-ydb
```

Для приложения нужны:

```env
YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
YDB_DATABASE=/ru-central1/<cloud_id>/<database_id>
YDB_METADATA_CREDENTIALS=1
```

`grpcs` — это gRPC поверх TLS. Для local-ydb используется `grpc`.

## Service account и доступ к YDB

Создать service account:

```bash
yc iam service-account create --name max-bot-sa
yc iam service-account get max-bot-sa
```

Сохранить id:

```bash
SA_ID=<service_account_id>
```

Выдать доступ к YDB:

```bash
yc ydb database add-access-binding max-bot-ydb \
  --role ydb.editor \
  --service-account-id "$SA_ID"
```

Для текущего проекта `ydb.editor` на конкретную базу проще и понятнее, чем широкая роль на весь folder. Если проект уйдет в строгий продакшен, права можно ужать отдельным анализом операций.

## Создание Cloud Function

Создать функцию:

```bash
yc serverless function create --name max-bot
```

Сделать функцию публичной:

```bash
yc serverless function allow-unauthenticated-invoke max-bot
```

Почему публичной: MAX должен вызвать webhook без Yandex IAM-подписи. Защита от чужих запросов делается через `WEBHOOK_SECRET`, который MAX передает в заголовке `X-Max-Bot-Api-Secret`.

Получить URL функции:

```bash
yc serverless function get max-bot
```

URL будет вида:

```text
https://functions.yandexcloud.net/<function_id>
```

Его надо указать в `WEBHOOK_URL` и в подписке MAX.

## Продакшен `.env`

Минимальный набор:

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
REMINDER_SYNC_INTERVAL_MINUTES=60
REMINDER_SYNC_WINDOW_MINUTES=5
PERFORMANCE_METRICS_ENABLED=true
PERFORMANCE_METRICS_SLOW_MS=1000
DOCUMENTS_VERSION=hackathon-2026-05
```

Секреты нельзя коммитить. `.env` нужен локальному deploy-скрипту, чтобы передать разрешенные переменные в новую версию Cloud Function.

`scripts/deploy-yc.ps1` и `scripts/deploy-yc.sh` передают только фиксированный список переменных. Если добавите новую настройку приложения, проверьте deploy-скрипты.

## Сборка пакета

Windows:

```powershell
.\scripts\build-yc-package.ps1
```

Linux:

```bash
bash scripts/build-yc-package.sh
```

Скрипт создает:

```text
dist/yc-package
dist/max-bot-yc.zip
```

В пакет попадают:

- `app`;
- `seed`;
- `index.py`;
- `requirements.txt`.

Из пакета удаляется:

```text
app/migration
```

Миграция тянет PostgreSQL/SQLAlchemy-зависимости, а runtime функции они не нужны.

## Деплой новой версии

Windows:

```powershell
.\scripts\deploy-yc.ps1 `
  -FunctionName max-bot `
  -ServiceAccountId "<service_account_id>"
```

Linux:

```bash
bash scripts/deploy-yc.sh max-bot "$SA_ID"
```

Скрипт:

1. собирает пакет;
2. читает `.env` и переменные окружения;
3. проверяет, что есть `MAX_BOT_TOKEN` и `WEBHOOK_SECRET`;
4. при `STORAGE_BACKEND=ydb` получает IAM-токен через `yc iam create-token`;
5. запускает `python -m app.ydb_schema`;
6. создает новую версию Cloud Function.

Параметры версии:

```text
runtime: python312
entrypoint: index.handler
memory: 512m
timeout: 30s
source-path: dist/yc-package
```

Создание новой версии не меняет URL функции. MAX продолжает ходить на тот же `https://functions.yandexcloud.net/<function_id>`.

## Проверка версии и тега latest

После деплоя проверьте список версий:

```bash
yc serverless function version list --function-name max-bot
```

Новая версия должна иметь тег `$latest`. Подробно посмотреть версию:

```bash
yc serverless function version get <version_id>
```

Health:

```bash
curl https://functions.yandexcloud.net/<function_id>
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

Логи:

```bash
yc serverless function logs max-bot --since 10m --limit 100
```

## Timer trigger для уведомлений

Уведомления отправляет timer trigger. Он вызывает ту же функцию, но событие приходит не как HTTP-запрос, а в формате timer-сообщения.

Создать trigger:

```bash
yc serverless trigger create timer max-bot-notifications \
  --cron-expression "*/5 * * * ? *" \
  --invoke-function-name max-bot \
  --invoke-function-service-account-id "$SA_ID"
```

Выражение `*/5 * * * ? *` означает запуск каждые 5 минут. В документации Yandex Cloud cron-поля идут так: минуты, часы, день месяца, месяц, день недели, год. Расписание timer работает в UTC+0.

Официальная документация:

- [Timer that invokes a Cloud Functions function](https://yandex.cloud/en/docs/functions/concepts/trigger/timer)
- [Creating a timer that invokes Cloud Functions](https://yandex.cloud/en/docs/functions/operations/trigger/timer-create)

Важно: service account trigger должен иметь право вызвать функцию. Документация Yandex Cloud указывает роль `functions.functionInvoker` для service account, который запускает функцию через timer.

## Что делает `index.handler`

Корневой `index.py`:

```python
handler = create_function_handler()
```

Yandex Cloud Functions вызывает `index.handler`.

Внутри `app/function_handler.py`:

- обычный `GET` возвращает `{"status":"ok"}`;
- `POST` проверяет secret, разбирает webhook MAX и вызывает dispatcher;
- timer event запускает `NotificationWorker.process_due`;
- `_CleanupScheduler` периодически чистит старые мероприятия;
- `_AsyncRunner` держит постоянный event loop для асинхронного MAX-клиента.

## Регистрация webhook в MAX

После создания функции зарегистрируйте webhook в MAX:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://functions.yandexcloud.net/<function_id>",
    "update_types": ["bot_started", "message_created", "message_callback"],
    "secret": "<WEBHOOK_SECRET>"
  }'
```

Официальная документация MAX по `POST /subscriptions`: [dev.max.ru/docs-api/methods/POST/subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions).

Проверьте текущие подписки:

```bash
curl -X GET "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>"
```

## Обновление продакшена

Стандартный порядок:

1. Внести изменения.
2. Прогнать тесты:

```bash
python -m pytest -q
```

3. Если менялись файлы приложения, собрать пакет:

```powershell
.\scripts\build-yc-package.ps1
```

4. Точечно проверить, что изменение попало в пакет:

```powershell
rg "нужная_строка_или_имя" dist/yc-package
```

5. Задеплоить:

```powershell
.\scripts\deploy-yc.ps1 -FunctionName max-bot -ServiceAccountId "<service_account_id>"
```

6. Проверить `$latest`, health, логи и один живой сценарий в MAX.

Если менялась схема YDB, отдельно убедитесь, что `python -m app.ydb_schema` выполнен на нужной базе. Deploy-скрипт делает это автоматически для `STORAGE_BACKEND=ydb`, но при ручных операциях лучше проверить явно.

## Логи и диагностика

Последние логи:

```bash
yc serverless function logs max-bot --since 30m --limit 100
```

PowerShell с явным путем:

```powershell
& "$env:USERPROFILE\yandex-cloud\bin\yc.exe" `
  serverless function logs max-bot `
  --since 30m `
  --limit 100
```

На что смотреть:

- `START`, `END`, `REPORT` — функция вызвалась и завершилась;
- `Duration` — длительность вызова на стороне Cloud Functions;
- `Function Init Duration` — cold start;
- строки с `"event":"perf_metric"` — внутренняя метрика приложения;
- stack trace — авария в коде;
- memory used — если близко к лимиту, увеличьте memory.

Подробнее про метрики и cold start: [performance-audit.md](performance-audit.md).

## Частые ошибки

`yc` не найден.

Добавьте `yc` в `PATH` или используйте полный путь. PowerShell-скрипт уже пробует fallback `C:\Users\<user>\yandex-cloud\bin\yc.exe`.

Функция не видит новые переменные.

Переменные окружения привязаны к версии функции. Если поменяли `.env`, нужно создать новую версию через deploy-скрипт.

Функция не имеет доступа к YDB.

Проверьте service account версии функции и access binding на YDB. Роль должна быть выдана именно тому service account, который указан в версии.

Webhook возвращает `403`.

Не совпадает `WEBHOOK_SECRET` в Cloud Function и подписке MAX.

Health работает, а POST webhook падает.

`GET` почти не ходит во внешние сервисы. `POST` обращается к YDB и MAX API. Смотрите логи, `MAX_BOT_TOKEN`, YDB credentials и traceback.

После деплоя MAX ведет себя по-старому.

Проверьте, что новая версия получила `$latest`, а подписка MAX указывает на нужную функцию.

Cold start занимает несколько секунд.

Это нормальная особенность serverless: после простоя Cloud Functions заново поднимает runtime, импортирует зависимости, создает YDB driver и MAX client. Смотрите `Function Init Duration` в `REPORT`.

## Стоимость

Serverless не значит “бесконечно бесплатно”. Расходы зависят от вызовов функции, длительности, памяти, чтений и записей YDB, хранения и логов.

Минимальные меры:

- держать `sls-provisioned-rcu` равным `0`, если не нужен гарантированный прогрев YDB;
- не хранить бинарные файлы в YDB;
- не делать слишком частый timer без причины;
- включить бюджетные уведомления в Billing;
- следить за графиками Cloud Functions и YDB;
- держать метрики производительности включенными хотя бы на период пилота.
