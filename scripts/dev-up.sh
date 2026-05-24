#!/usr/bin/env bash
set -euo pipefail

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Создан .env из .env.example. Заполните MAX_BOT_TOKEN и WEBHOOK_SECRET."
fi

if [ "${1:-}" = "--build" ]; then
  docker compose up --build -d
else
  docker compose up -d
fi

docker compose ps

