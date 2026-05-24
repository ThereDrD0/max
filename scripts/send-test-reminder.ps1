param(
    [Parameter(Mandatory = $true)]
    [long]$ActorUserId,

    [Parameter(Mandatory = $true)]
    [long]$EventId,

    [int]$Minutes = 60,

    [Nullable[long]]$SlotId = $null,

    [switch]$SendNow
)

$ErrorActionPreference = "Stop"

$pythonArgs = @(
    "-m",
    "app.test_reminder",
    "--actor-user-id",
    $ActorUserId,
    "--event-id",
    $EventId,
    "--minutes",
    $Minutes
)

if ($null -ne $SlotId) {
    $pythonArgs += @("--slot-id", $SlotId)
}

if ($SendNow) {
    $pythonArgs += "--send-now"
}

python @pythonArgs
