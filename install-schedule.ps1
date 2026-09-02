<#
.SYNOPSIS
  Register (or remove) the Ralph tick in Windows Task Scheduler.

.DESCRIPTION
  The plan gates this on phases 1-5 passing by hand, so this script does NOT run
  itself. Read it, satisfy the checklist it prints, then run it deliberately.

  The tick is driven by Task Scheduler rather than cron because the whole
  toolchain (Claude Code, Ollama on the GPU, git credentials) is installed
  natively on Windows; running under WSL2 would need a second Claude install and
  a second `claude login`.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install-schedule.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File install-schedule.ps1
  powershell -ExecutionPolicy Bypass -File install-schedule.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "Ralph tick",
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

# --- refuse to install until the loop is actually safe to run unattended -----
# Must be GIT Bash, not WSL. On a default Windows install `bash.exe` on PATH is
# C:\Windows\System32\bash.exe (the WSL shim); a task pointed at that would run
# dispatch.sh inside WSL, where claude, the git credential store and Ollama do
# not exist. Prefer the Git for Windows install and reject the WSL shim.
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
Write-Host "Using shell: $bash" -ForegroundColor Cyan

if ($env:ANTHROPIC_API_KEY) {
    throw "ANTHROPIC_API_KEY is set. Ralph runs on the Claude Pro subscription; a present API key bills per-token. Unset it before scheduling."
}

Write-Host "Running preflight..." -ForegroundColor Cyan
& python (Join-Path $here "preflight.py")
if ($LASTEXITCODE -ne 0) { throw "preflight failed; refusing to install the schedule." }

Write-Host @"

Before you enable this, confirm by hand (plan phases 1-5):
  [ ] a tagged ticket produces a branch, a PR left in review, and a Slack message
  [ ] the Slack buttons work and '/ralph stop' halts the next tick
  [ ] the target repo's test suite is GREEN on its default branch
  [ ] PC sleep is disabled, or the tick will not fire while you are away

"@ -ForegroundColor Yellow

# --- the task ----------------------------------------------------------------
# One trigger per hour inside the configured windows. dispatch.sh is itself the
# authority on whether a given tick does anything: it re-checks the window, the
# per-day cap and the STOP file, so extra triggers are harmless.
$dispatch = Join-Path $here "dispatch.sh"
$unixPath = (& $bash -c "cygpath -u '$dispatch'").Trim()

$action = New-ScheduledTaskAction -Execute $bash -Argument "-lc `"$unixPath`"" -WorkingDirectory $here

$triggers = @()
foreach ($hour in 1, 2, 3, 4, 5, 12) {
    $triggers += New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($hour))
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Runs as the logged-in user: the subscription OAuth token and git credentials
# live in this user's profile, so SYSTEM would not have them.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered '$TaskName' (hourly at 01-05 and 12)." -ForegroundColor Green
    Write-Host "Pause any time with: python runs.py stop" -ForegroundColor Green
}
