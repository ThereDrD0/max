param(
    [string]$Url = "http://localhost:8080/webhook"
)

$ErrorActionPreference = "Stop"
$secret = "change_me_secret"
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        if ($_ -match "^WEBHOOK_SECRET=(.*)$") {
            $script:secret = $Matches[1]
        }
    }
}

$body = @{
    update_type = "bot_started"
    chat_id = 9001
    user = @{
        user_id = 101
        name = "Локальный пользователь"
        is_bot = $false
    }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Post `
    -Uri $Url `
    -Headers @{"X-Max-Bot-Api-Secret" = $secret} `
    -ContentType "application/json" `
    -Body $body

