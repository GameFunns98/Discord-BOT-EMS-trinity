param(
    [string]$InstallDirectory = "",
    [switch]$SkipAutostart
)

$ErrorActionPreference = "Stop"

$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceApplication = Join-Path $sourceDirectory "TicketRenamerTray.exe"
$sourceEnvironment = Join-Path $sourceDirectory ".env"
$sourceEnvironmentExample = Join-Path $sourceDirectory ".env.example"
$sourceReadme = Join-Path $sourceDirectory "README.md"

if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "Windows neposkytl cestu LOCALAPPDATA."
    }
    $InstallDirectory = Join-Path $env:LOCALAPPDATA "DiscordTicketRenamer"
}

$InstallDirectory = [System.IO.Path]::GetFullPath($InstallDirectory)
$installedApplication = Join-Path $InstallDirectory "TicketRenamerTray.exe"
$installedEnvironment = Join-Path $InstallDirectory ".env"
$installedEnvironmentExample = Join-Path $InstallDirectory ".env.example"
$installedReadme = Join-Path $InstallDirectory "README.md"

if (-not (Test-Path -LiteralPath $sourceApplication)) {
    throw "Chybi TicketRenamerTray.exe. Rozbalte znovu kompletni balicek."
}

$runningInstalledApplication = @(
    Get-Process -Name "TicketRenamerTray" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $installedApplication }
)
if ($runningInstalledApplication.Count -gt 0) {
    throw "Aplikace je spustena. Ukoncete ji pres tray menu a spusťte setup.ps1 znovu."
}

New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null

if ($sourceApplication -ne $installedApplication) {
    Copy-Item -LiteralPath $sourceApplication -Destination $installedApplication -Force
}
Copy-Item -LiteralPath $sourceEnvironmentExample -Destination $installedEnvironmentExample -Force
Copy-Item -LiteralPath $sourceReadme -Destination $installedReadme -Force

if (-not (Test-Path -LiteralPath $installedEnvironment)) {
    if (Test-Path -LiteralPath $sourceEnvironment) {
        Copy-Item -LiteralPath $sourceEnvironment -Destination $installedEnvironment
        Write-Host "Byla prenesena vase existujici konfigurace .env."
    }
    else {
        Copy-Item -LiteralPath $sourceEnvironmentExample -Destination $installedEnvironment
        Write-Host "Byl vytvoren soubor .env. Doplnte do nej DISCORD_BOT_TOKEN."
    }
}
else {
    Write-Host "Existujici konfigurace .env byla zachovana."
}

# Pri aktualizaci doplnte pouze chybejici klice z nove sablony. Existujici
# tokeny a jine hodnoty se nikdy neprepisuji.
$existingEnvironmentLines = @(Get-Content -LiteralPath $installedEnvironment)
$existingEnvironmentKeys = @{}
foreach ($line in $existingEnvironmentLines) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $existingEnvironmentKeys[$Matches[1]] = $true
    }
}

$missingEnvironmentLines = @()
foreach ($line in @(Get-Content -LiteralPath $sourceEnvironmentExample)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $settingName = $Matches[1]
        if (-not $existingEnvironmentKeys.ContainsKey($settingName)) {
            $missingEnvironmentLines += $line
            $existingEnvironmentKeys[$settingName] = $true
        }
    }
}

if ($missingEnvironmentLines.Count -gt 0) {
    Add-Content -LiteralPath $installedEnvironment -Value "" -Encoding utf8
    Add-Content -LiteralPath $installedEnvironment -Value "# Nastaveni doplnena aktualizaci aplikace" -Encoding utf8
    Add-Content -LiteralPath $installedEnvironment -Value $missingEnvironmentLines -Encoding utf8
    Write-Host "Do .env byla doplnena chybejici nastaveni bez prepsani existujicich hodnot."
}

if (-not $SkipAutostart) {
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $autostartCommand = '"' + $installedApplication + '"'
    New-Item -Path $runKey -Force | Out-Null
    New-ItemProperty -Path $runKey -Name "DiscordTicketRenamer" -PropertyType String -Value $autostartCommand -Force | Out-Null
}

Write-Host ""
Write-Host "Discord Ticket Renamer je pripraven v:"
Write-Host $InstallDirectory
if ($SkipAutostart) {
    Write-Host "Automaticke spusteni bylo pro tento test preskoceno."
}
else {
    Write-Host "Automaticke spusteni po prihlaseni do Windows je zapnute."
}
Write-Host "Zkontrolujte v .env Discord token, FiveRoster API klic a UUID rosteru."
Write-Host "Pote spustte start-bot.ps1 z rozbaleneho balicku."
