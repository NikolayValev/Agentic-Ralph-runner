<#
.SYNOPSIS
  Register (or remove) the Ralph Slack listener as a logon-triggered task.

.DESCRIPTION
  The tick (install-schedule.ps1) is a batch job; this is a daemon. It must be
  up whenever you might want to press a button or type `/ralph stop`, which is
  precisely when you are NOT at the machine -- so it starts at logon and is
  restarted by Task Scheduler if the socket drops.

  Runs as the logged-in user for the same reason the tick does: the Slack
  tokens live in .env under this profile and git credentials are per-user.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-listener.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File install-listener.ps1
  powershell -ExecutionPolicy Bypass -File install-listener.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "Ralph listener",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

# Must be GIT Bash, not WSL -- see the same note in install-schedule.ps1.
$bash = @(
    "$env:ProgramFiles\Git\bin\bash.exe",
    "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
    "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $bash) {
    $onPath = (Get-Command bash.exe -ErrorAction SilentlyContinue).Source
    if ($onPath -and $onPath -notlike "*\System32\*" -and $onPath -notlike "*\SysWOW64\*") {
        $bash = $onPath
    }
}
if (-not $bash) { throw "Git Bash not found. Install Git for Windows; the WSL bash.exe in System32 will not work." }

# Both Slack tokens must be present, or the listener would boot-loop: Task
# Scheduler would restart it every minute against a config that cannot work.
$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
foreach ($key in "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") {
    if (-not (Select-String -Path $envFile -Pattern "^$key=." -Quiet)) {
        throw "$key is not set in .env; the listener cannot start without it."
    }
}

$script   = Join-Path $here "listener.sh"
$unixPath = (& $bash -c "cygpath -u '$script'").Trim()
if (-not $unixPath) { throw "cygpath failed to convert $script" }

$action  = New-ScheduledTaskAction -Execute $bash -Argument "-lc `"$unixPath`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit 0 = run forever; the default 3 days would kill the daemon.
# RestartInterval/Count cover a dropped websocket that slack_sdk cannot recover.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 3 `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered '$TaskName' (starts at logon, restarts on failure)." -ForegroundColor Green
    Write-Host "Start it now with: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
    Write-Host "Logs: $here\logs\listener-<date>.log" -ForegroundColor Green
}
