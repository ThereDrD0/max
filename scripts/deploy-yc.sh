#!/usr/bin/env bash
set -euo pipefail

function_name="${1:-max-bot}"
service_account_id="${2:-}"

if [ -z "$service_account_id" ]; then
  echo "Использование: scripts/deploy-yc.sh <function-name> <service-account-id>" >&2
  exit 1
fi

"$(dirname "$0")/build-yc-package.sh"

set -a
if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi
set +a

: "${MAX_BOT_TOKEN:?Не задан MAX_BOT_TOKEN в .env или окружении}"
: "${WEBHOOK_SECRET:?Не задан WEBHOOK_SECRET в .env или окружении}"

apply_ydb_schema() {
  local backend="${STORAGE_BACKEND:-ydb}"
  if [ "$backend" != "ydb" ]; then
    return
  fi

  local previous_token="${YDB_ACCESS_TOKEN_CREDENTIALS:-}"
  if [ -z "$previous_token" ]; then
    export YDB_ACCESS_TOKEN_CREDENTIALS
    YDB_ACCESS_TOKEN_CREDENTIALS="$(yc iam create-token)"
    python -m app.ydb_schema
    unset YDB_ACCESS_TOKEN_CREDENTIALS
  else
    python -m app.ydb_schema
  fi
}

apply_ydb_schema

env_pairs=()
escape_yc_env() {
  local value="${1//\\/\\\\}"
  printf '%s' "${value//,/\\,}"
}

for key in \
  MAX_BOT_TOKEN \
  MAX_BOT_USERNAME \
  WEBHOOK_SECRET \
  WEBHOOK_PATH \
  STORAGE_BACKEND \
  YDB_ENDPOINT \
  YDB_DATABASE \
  YDB_METADATA_CREDENTIALS \
  ADMIN_USER_IDS \
  ORGANIZER_USER_IDS \
  MAX_API_RPS \
  REMINDER_SYNC_INTERVAL_MINUTES \
  REMINDER_SYNC_WINDOW_MINUTES \
  PERFORMANCE_METRICS_ENABLED \
  PERFORMANCE_METRICS_SLOW_MS \
  DOCUMENTS_VERSION \
  APP_ENV
do
  value="${!key:-}"
  if [ -n "$value" ]; then
    env_pairs+=("$key=$(escape_yc_env "$value")")
  fi
done

environment="$(IFS=,; echo "${env_pairs[*]}")"

yc serverless function version create \
  --function-name "$function_name" \
  --runtime python312 \
  --entrypoint index.handler \
  --memory 128m \
  --execution-timeout 10s \
  --service-account-id "$service_account_id" \
  --source-path ./dist/yc-package \
  --environment "$environment" >/dev/null

echo "Версия функции $function_name загружена."
