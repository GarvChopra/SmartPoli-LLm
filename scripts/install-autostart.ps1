# Registers R2 to start automatically when you log in to Windows.
# Runs as your normal user (no admin rights). Remove it with uninstall-autostart.ps1.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Cannot find $pythonw. Create the environment first: python -m venv .venv ; .venv\Scripts\python -m pip install -e ."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m r2.daemon" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "R2 Assistant" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "R2 will now start at login. To start it right now: Start-ScheduledTask -TaskName 'R2 Assistant'"
