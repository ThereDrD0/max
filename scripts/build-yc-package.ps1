$ErrorActionPreference = "Stop"

$root = (Resolve-Path ".").Path
$dist = Join-Path $root "dist"
$stage = Join-Path $dist "yc-package"
$archive = Join-Path $dist "max-bot-yc.zip"

if (-not (Test-Path $dist)) {
    New-Item -ItemType Directory -Path $dist | Out-Null
}

$resolvedDist = (Resolve-Path $dist).Path
if (-not $stage.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Небезопасный путь staging-каталога: $stage"
}

if (Test-Path $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null

Copy-Item -Path (Join-Path $root "app") -Destination (Join-Path $stage "app") -Recurse
Copy-Item -Path (Join-Path $root "seed") -Destination (Join-Path $stage "seed") -Recurse
Copy-Item -Path (Join-Path $root "index.py") -Destination $stage
Copy-Item -Path (Join-Path $root "requirements.txt") -Destination $stage

$migrationPath = Join-Path $stage "app\migration"
if (Test-Path $migrationPath) {
    Remove-Item -LiteralPath $migrationPath -Recurse -Force
}
Get-ChildItem -Path $stage -Directory -Recurse -Filter "__pycache__" | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
}
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive -Force
Write-Host "Пакет Cloud Functions создан: $archive"
