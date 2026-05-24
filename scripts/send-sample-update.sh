#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://localhost:8080/webhook}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-change_me_secret}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

curl -sS -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "X-Max-Bot-Api-Secret: $WEBHOOK_SECRET" \
  -d '{
    "update_type": "bot_started",
    "chat_id": 9001,
    "user": {
      "user_id": 101,
      "name": "Локальный пользователь",
      "is_bot": false
    }
  }'

