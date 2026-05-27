# Серверная часть в Yandex Cloud

Продакшен-вариант проекта развёрнут без собственного сервера: входящие webhook-запросы принимает Yandex Cloud Functions, данные лежат в YDB Serverless, а автоматические уведомления запускаются timer trigger. Docker остаётся для локальной разработки.

## Какие сервисы используются

Используются:

- Yandex Cloud Functions — публичный HTTPS endpoint для MAX webhook и обработчик timer-событий.
- YDB Serverless — основная база данных.
- Service account — техническая учётная запись, от имени которой функция ходит в YDB.
- Timer trigger — периодический запуск функции для отправки уведомлений из `notification_outbox`.

Сознательно не используются:

- API Gateway: прямого URL Cloud Functions достаточно, а API Gateway добавляет ещё один сервис и потенциальную тарификацию.
- Container Registry: функция деплоится исходниками/пакетом Python, без контейнерного образа.
- VM/VPS: нет постоянно работающего сервера, который надо покупать, обновлять и администрировать.

## Текущий стенд

Нечувствительные параметры текущего стенда:

```text
Cloud Function name: max-bot
Cloud Function public URL: https://functions.yandexcloud.net/d4en27dqbb87rvb7lmms
YDB name: ydb563
YDB endpoint: grpcs://ydb.serverless.yandexcloud.net:2135
YDB database: /ru-central1/b1girouj93o3e1m15oh4/etnjer5cu9onu833cnlt
Service account name: max-bot-sa
Timer trigger name: max-bot-notifications
```

Не вставляйте в документацию:

- `MAX_BOT_TOKEN`;
- `WEBHOOK_SECRET`;
- OAuth/IAM-токены;
- файл ключа сервисного аккаунта, если когда-нибудь будете его использовать.

## Сайт для управления

Авторизоваться нужно в Yandex Cloud Console:

```text
https://console.yandex.cloud/
```

Основные разделы:

- Cloud Functions — функция `max-bot`, версии, переменные окружения, логи.
- Managed Service for YDB — база `ydb563`, endpoint, путь базы, таблицы, запросы.
- Service accounts — сервисный аккаунт `max-bot-sa`.
- Cloud Functions -> Triggers — timer trigger `max-bot-notifications`.
- Billing — бюджетные уведомления и контроль расходов.

## Установка и настройка `yc`

`yc` — это командная строка Yandex Cloud. На этой машине она может лежать здесь:

```text
C:\Users\drdth\yandex-cloud\bin\yc.exe
```

Скрипт `scripts/deploy-yc.ps1` сначала ищет `yc` в `PATH`, а если не находит, использует этот fallback-путь.

Проверка:

```powershell
& "$env:USERPROFILE\yandex-cloud\bin\yc.exe" config list
```

или, если `yc` есть в `PATH`:

```bash
yc config list
```

При первичной настройке:

```bash
yc init
```

Команда спросит cloud, folder и зону Compute. Для этого проекта Compute zone почти не важна, потому что виртуальные машины не используются. Можно выбрать ближайшую стандартную зону или пропустить, если CLI позволяет.

## Создание инфраструктуры через `yc`

Создать YDB Serverless:

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

Создать Cloud Function:

```bash
yc serverless function create --name max-bot
```

Сделать функцию публичной:

```bash
yc serverless function allow-unauthenticated-invoke max-bot
```

Почему публичной? MAX должен вызвать webhook без Yandex IAM-подписи. Защита от чужих запросов делается через `WEBHOOK_SECRET`.

Создать timer trigger:

```bash
yc serverless trigger create timer max-bot-notifications \
  --cron-expression "*/5 * * * ? *" \
  --invoke-function-name max-bot \
  --invoke-function-service-account-id "$SA_ID"
```

`*/5 * * * ? *` означает запуск каждые 5 минут. Timer вызывает ту же функцию, но не как HTTP webhook: handler видит специальное timer-событие и запускает `NotificationWorker`.

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
ORGANIZER_USER_IDS=<MAX user id организаторов через запятую>
MAX_API_RPS=30
DOCUMENTS_VERSION=hackathon-2026-05
```

Подробно:

| Переменная | Продакшен-значение | Где брать | Нюансы |
| --- | --- | --- | --- |
| `APP_ENV` | `prod` | Задаём вручную. | В `prod` бот скрывает dev-информацию вроде raw id мероприятий. |
| `MAX_BOT_TOKEN` | секрет | В настройках бота MAX. | Нужен для исходящих запросов. Не коммитить. |
| `MAX_BOT_USERNAME` | ник без `@` | Публичная ссылка бота `https://max.ru/<botName>`. | Лучше задать явно для ссылок в карточках. Если пусто, бот попробует получить ник через MAX API `/me`; входящие диплинки работают и без него. |
| `WEBHOOK_URL` | `https://functions.yandexcloud.net/<function_id>` | `yc serverless function get max-bot`. | Удобно хранить для регистрации подписки MAX. Код функции не строит логику на этой переменной. |
| `WEBHOOK_SECRET` | секрет | Генерируем сами. | Должен совпадать с secret в подписке MAX. |
| `WEBHOOK_PATH` | `/webhook` | Значение по умолчанию. | В Cloud Functions прямой URL попадает в `index.handler`; путь критичен для локального FastAPI. |
| `STORAGE_BACKEND` | `ydb` | Задаём вручную. | `memory` только для тестов. |
| `YDB_ENDPOINT` | `grpcs://ydb.serverless.yandexcloud.net:2135` | Страница YDB в консоли. | В облаке нужен `grpcs`, не `grpc`. |
| `YDB_DATABASE` | `/ru-central1/.../...` | Страница YDB в консоли. | Это полный путь базы, не короткое имя `ydb563`. |
| `YDB_METADATA_CREDENTIALS` | `1` | Задаём вручную. | Функция получает IAM-токен service account через metadata service. |
| `SOURCE_DATABASE_URL` | пусто | Нужно только для миграции. | Не передавайте старую БД в обычный продакшен-запуск. |
| `ADMIN_USER_IDS` | id через запятую | MAX user id администраторов. | Админ видит все мероприятия. |
| `ORGANIZER_USER_IDS` | id через запятую | MAX user id организаторов. | При seed-загрузке получают доступ к мероприятиям. |
| `MAX_API_RPS` | `30` | Выбирается вручную. | Ограничение отправки уведомлений в MAX API. |
| `DOCUMENTS_VERSION` | например `hackathon-2026-05` | Версия текста согласия. | При изменении юридического текста увеличивайте версию. |

## Сборка пакета функции

Windows:

```powershell
.\scripts\build-yc-package.ps1
```

Linux:

```bash
bash scripts/build-yc-package.sh
```

Скрипт создаёт:

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

Это сделано, чтобы runtime функции не тянул PostgreSQL/SQLAlchemy-зависимости, которые нужны только для миграции.

## Деплой функции

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
2. читает `.env`;
3. применяет YDB-схему через `python -m app.ydb_schema`;
4. передаёт разрешённые переменные окружения в Cloud Functions;
5. создаёт новую версию функции.

Параметры версии:

```text
runtime: python312
entrypoint: index.handler
memory: 512m
timeout: 30s
source-path: dist/yc-package
```

Нюанс: создание новой версии не меняет URL функции. MAX webhook продолжает ходить на тот же `https://functions.yandexcloud.net/<function_id>`.

## Что лежит в `index.py`

Корневой файл `index.py` экспортирует:

```python
handler = create_function_handler()
```

Yandex Cloud Functions вызывает `index.handler`. Внутри handler:

- `GET` возвращает health JSON;
- `POST` проверяет webhook secret и обрабатывает MAX update;
- timer event запускает worker уведомлений.

В `app/function_handler.py` есть постоянный `_AsyncRunner`. Он нужен, чтобы не закрывать event loop после каждого вызова: библиотека `maxapi` хранит асинхронную HTTP-сессию, и при закрытом loop исходящие запросы начинают падать.

## Обновление продакшена

Стандартный порядок:

1. Внести изменения локально.
2. Прогнать тесты:

```bash
python -m pytest -q
```

3. Если менялась YDB-схема, обновить её:

```powershell
$yc = Join-Path $env:USERPROFILE "yandex-cloud\bin\yc.exe"
$env:YDB_ACCESS_TOKEN_CREDENTIALS = & $yc iam create-token
python -m app.ydb_schema
Remove-Item Env:\YDB_ACCESS_TOKEN_CREDENTIALS
```

или:

```bash
export YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
python -m app.ydb_schema
unset YDB_ACCESS_TOKEN_CREDENTIALS
```

4. Загрузить новую версию:

```powershell
.\scripts\deploy-yc.ps1 -FunctionName max-bot -ServiceAccountId "<service_account_id>"
```

или:

```bash
bash scripts/deploy-yc.sh max-bot "$SA_ID"
```

5. Проверить health:

```bash
curl https://functions.yandexcloud.net/<function_id>
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

6. Проверить логи:

```bash
yc serverless function logs max-bot --since 10m --limit 100
```

7. Проверить сценарий в MAX: `/start`, кнопка информации, запись, мои записи.

## Логи и диагностика

Команда:

```bash
yc serverless function logs max-bot --since 30m --limit 100
```

PowerShell с явным путём:

```powershell
& "$env:USERPROFILE\yandex-cloud\bin\yc.exe" `
  serverless function logs max-bot `
  --since 30m `
  --limit 100
```

На что смотреть:

- `START`, `END`, `REPORT` — функция вызвалась и завершилась;
- `Duration` — сколько длился вызов;
- `Function Init Duration` — холодный старт;
- `ERROR` или stack trace — ошибка приложения;
- `Memory Used` — если близко к лимиту, увеличить memory.

Если команда логов зависает или долго держит процесс в PowerShell, можно остановить оставшийся `yc`:

```powershell
Get-Process yc -ErrorAction SilentlyContinue | Stop-Process -Force
```

## Проверка webhook вручную

Для проверки продакшен webhook без клиента MAX можно отправить тестовый POST. Не вставляйте настоящий secret в историю команд, если терминал логируется.

PowerShell:

```powershell
$headers = @{
  "Content-Type" = "application/json"
  "X-Max-Bot-Api-Secret" = "<WEBHOOK_SECRET>"
}

$body = @{
  update_type = "message_created"
  message = @{
    sender = @{ user_id = 123456789; name = "Тестовый пользователь" }
    recipient = @{}
    body = @{ text = "/start"; mid = "manual-check" }
  }
} | ConvertTo-Json -Depth 10 -Compress

Invoke-RestMethod `
  -Method Post `
  -Uri "https://functions.yandexcloud.net/<function_id>" `
  -Headers $headers `
  -Body $body
```

Если ответ:

```json
{"ok":true}
```

webhook обработал событие. Если при этом бот ничего не прислал в MAX, смотрите логи функции и корректность `MAX_BOT_TOKEN`.

## Timer trigger и уведомления

Напоминания и ручные уведомления не отправляются прямо в момент создания записи массовым циклом. Они попадают в:

```text
notification_outbox
```

Timer trigger вызывает функцию каждые 5 минут. Handler распознаёт timer event по типу:

```text
yandex.cloud.events.serverless.triggers.TimerMessage
```

и запускает `NotificationWorker`. Worker:

1. выбирает pending-уведомления, у которых `send_after <= now`;
2. проверяет, что уведомления по записи не отключены;
3. отправляет сообщение через MAX API;
4. помечает уведомление `sent`, `failed` или `skipped`;
5. соблюдает `MAX_API_RPS`.

Если уведомления не уходят:

- проверьте, существует ли trigger;
- проверьте, что trigger вызывает именно `max-bot`;
- проверьте service account trigger;
- посмотрите `notification_outbox`;
- посмотрите логи функции за время запуска trigger.

## Бесплатный режим

Чтобы держаться в нуле или около нуля:

- не используйте API Gateway без необходимости;
- не используйте Container Registry;
- не держите VM/VPS;
- YDB Serverless создавайте с `sls-provisioned-rcu 0`;
- ограничьте storage;
- включите бюджетные уведомления в Billing;
- следите за количеством вызовов функции;
- не запускайте слишком частый timer без причины;
- не храните большие бинарные данные в YDB.

Важно: тарифы и бесплатные лимиты могут меняться. Перед долгим публичным запуском проверьте актуальные условия в Yandex Cloud Billing.

## Частые ошибки

`yc` не найден.

Добавьте `yc` в `PATH` или используйте полный путь:

```powershell
& "$env:USERPROFILE\yandex-cloud\bin\yc.exe" config list
```

Функция не видит переменные окружения.

Переменные передаются при создании версии. Если вы поменяли `.env`, надо создать новую версию функции через deploy-скрипт.

Функция не имеет доступа к YDB.

Проверьте service account версии функции и access binding на YDB. Роль должна быть выдана именно тому service account, который указан в версии функции.

Health работает, а webhook падает.

`GET` почти не использует внешние сервисы. `POST` ходит в YDB и MAX API. Смотрите logs и проверяйте `WEBHOOK_SECRET`, YDB credentials и `MAX_BOT_TOKEN`.

После деплоя MAX всё ещё ведёт себя по-старому.

Проверьте, что новая версия функции действительно создана, а webhook URL указывает на эту функцию. URL функции не меняется, но если у вас несколько функций, MAX может быть подписан не на ту.

Холодный старт занимает несколько секунд.

Это нормально для Cloud Functions: первый запрос после простоя инициализирует Python, зависимости, YDB driver и MAX client. Последующие тёплые вызовы быстрее.

Ошибка `Event loop is closed`.

Проверьте, что в функции используется текущий `app/function_handler.py` с `_AsyncRunner`, и что `MaxApiBotClient` не переиспользуется между разными закрытыми event loop.
