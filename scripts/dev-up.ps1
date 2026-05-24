param(
    [switch]$Build
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env из .env.example. Заполните MAX_BOT_TOKEN и WEBHOOK_SECRET."
}

$args = @("compose", "up", "-d")
if ($Build) {
    $args = @("compose", "up", "--build", "-d")
}

docker @args
docker compose ps

