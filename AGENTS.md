# Локальные инструкции для Codex

Пиши по-русски, коротко и по делу.

Перед любыми git-командами сначала прочитай правила в [CONTRIBUTING.md](CONTRIBUTING.md). Используй их только для работы с git.

Перед ответом о готовности изменений для Яндекса всегда проверяй, что в `dist/yc-package` и `dist/max-bot-yc.zip` лежит актуальная версия кода. Если менялись файлы приложения, перед деплоем или советом деплоить нужно выполнить:

```powershell
.\scripts\build-yc-package.ps1
```

После пересборки проверь точечно, что нужные изменения попали в `dist/yc-package`, например через `rg`, и только потом говори, что пакет для Yandex Cloud Functions актуален.

Если работа закончена и менялись файлы приложения, обязательно задеплой новую версию в Yandex Cloud Functions штатным скриптом:

```powershell
.\scripts\deploy-yc.ps1 -FunctionName max-bot -ServiceAccountId "<service_account_id>"
```

Если `service_account_id` не передан пользователем, возьми его из текущей версии функции через `yc serverless function version get`. После деплоя проверь, что новая версия получила тег `$latest`, и сообщи id версии.

Если менялась схема YDB, отдельно напомни выполнить:

```powershell
python -m app.ydb_schema
```
