$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceApplication = Join-Path $sourceDirectory "TicketRenamerTray.exe"
$sourceEnvironment = Join-Path $sourceDirectory ".env"
$installedDirectory = Join-Path $env:LOCALAPPDATA "DiscordTicketRenamer"
$installedApplication = Join-Path $installedDirectory "TicketRenamerTray.exe"
$installedEnvironment = Join-Path $installedDirectory ".env"

if (Test-Path -LiteralPath $installedApplication) {
    $application = $installedApplication
    $environmentFile = $installedEnvironment
    $workingDirectory = $installedDirectory
}
elseif (Test-Path -LiteralPath $sourceApplication) {
    $application = $sourceApplication
    $environmentFile = $sourceEnvironment
    $workingDirectory = $sourceDirectory
}
else {
    throw "Chybi TicketRenamerTray.exe. Spustte nejprve setup.ps1."
}

if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "Chybi soubor .env. Spustte setup.ps1 a doplnte konfiguraci."
}

Start-Process -FilePath $application -WorkingDirectory $workingDirectory -WindowStyle Hidden
Write-Host "Discord Ticket Renamer byl spusten v oznamovaci oblasti u hodin."

