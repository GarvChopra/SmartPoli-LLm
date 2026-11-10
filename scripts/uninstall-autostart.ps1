# Removes the R2 auto-start entry and stops the running assistant.
$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName "R2 Assistant" -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName "R2 Assistant" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "R2 Assistant" -Confirm:$false
    Write-Host "R2 auto-start removed."
} else {
    Write-Host "R2 auto-start was not installed."
}
