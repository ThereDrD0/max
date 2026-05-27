param(
    [string]$FunctionName = "max-bot",
    [Parameter(Mandatory = $true)]
    [string]$ServiceAccountId
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "build-yc-package.ps1")

$ycCommand = Get-Command yc -ErrorAction SilentlyContinue
if ($ycCommand) {
    $yc = $ycCommand.Source
} else {
    $fallback = Join-Path $env:USERPROFILE "yandex-cloud\bin\yc.exe"
    if (-not (Test-Path $fallback)) {
        throw "Не найден yc CLI. Установите Yandex Cloud CLI или добавьте yc в PATH."
    }
    $yc = $fallback
}

$root = (Resolve-Path ".").Path
$stage = Join-Path $root "dist\yc-package"
$envFile = Join-Path $root ".env"
$values = @{}

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        $values[$parts[0]] = $parts[1]
    }
}

$keys = @(
    "MAX_BOT_TOKEN",
    "MAX_BOT_USERNAME",
    "WEBHOOK_SECRET",
    "WEBHOOK_PATH",
    "STORAGE_BACKEND",
    "YDB_ENDPOINT",
    "YDB_DATABASE",
    "YDB_METADATA_CREDENTIALS",
    "ADMIN_USER_IDS",
    "ORGANIZER_USER_IDS",
    "MAX_API_RPS",
    "REMINDER_SYNC_INTERVAL_MINUTES",
    "REMINDER_SYNC_WINDOW_MINUTES",
    "PERFORMANCE_METRICS_ENABLED",
    "PERFORMANCE_METRICS_SLOW_MS",
    "DOCUMENTS_VERSION",
    "APP_ENV"
)

$environment = @()
foreach ($key in $keys) {
    $value = $values[$key]
    if ($null -eq $value -or $value -eq "") {
        $value = [Environment]::GetEnvironmentVariable($key)
    }
    if ($null -ne $value -and $value -ne "") {
        $escapedValue = $value.Replace("\", "\\").Replace(",", "\,")
        $environment += "$key=$escapedValue"
    }
}

if (-not ($environment -match "^MAX_BOT_TOKEN=")) {
    throw "Не задан MAX_BOT_TOKEN в .env или переменных окружения"
}
if (-not ($environment -match "^WEBHOOK_SECRET=")) {
    throw "Не задан WEBHOOK_SECRET в .env или переменных окружения"
}

function Invoke-YdbSchemaMigration {
    $backend = $values["STORAGE_BACKEND"]
    if ([string]::IsNullOrWhiteSpace($backend)) {
        $backend = [Environment]::GetEnvironmentVariable("STORAGE_BACKEND")
    }
    if ([string]::IsNullOrWhiteSpace($backend)) {
        $backend = "ydb"
    }
    if ($backend.ToLowerInvariant() -ne "ydb") {
        return
    }

    $previousToken = [Environment]::GetEnvironmentVariable("YDB_ACCESS_TOKEN_CREDENTIALS")
    $createdToken = $false
    if ([string]::IsNullOrWhiteSpace($previousToken)) {
        $env:YDB_ACCESS_TOKEN_CREDENTIALS = (& $yc iam create-token).Trim()
        $createdToken = $true
    }

    try {
        python -m app.ydb_schema
    } finally {
        if ($createdToken) {
            Remove-Item Env:\YDB_ACCESS_TOKEN_CREDENTIALS -ErrorAction SilentlyContinue
        }
    }
}

Invoke-YdbSchemaMigration

& $yc serverless function version create `
    --function-name $FunctionName `
    --runtime python312 `
    --entrypoint index.handler `
    --memory 512m `
    --execution-timeout 30s `
    --service-account-id $ServiceAccountId `
    --source-path $stage `
    --environment ($environment -join ",") | Out-Null

Write-Host "Function version uploaded: $FunctionName"
