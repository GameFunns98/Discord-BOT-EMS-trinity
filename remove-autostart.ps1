$ErrorActionPreference = "Stop"

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$valueName = "DiscordTicketRenamer"

if (Test-Path -Path $runKey) {
    Remove-ItemProperty -Path $runKey -Name $valueName -ErrorAction SilentlyContinue
}

Write-Host "Automaticke spusteni Discord Ticket Renamer bylo vypnuto."
