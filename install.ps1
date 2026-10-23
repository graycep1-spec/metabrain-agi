<#
Установка MetaBrain на Windows 10/11 с Docker Desktop (WSL 2). Ставится в папку, где лежит скрипт.
Повторный запуск — обновление: конфигурация (deploy\.env) и данные (том metabrain_pgdata) сохраняются.

  .\install.ps1                      установить или обновить, спросить ключи, которых нет
  .\install.ps1 -Action configure    поменять ключи: DeepSeek, бот, адрес кабинета, вход через Telegram
  .\install.ps1 -Action status       что запущено и какие ключи заданы (без значений)
  .\install.ps1 -Action tokens       токены менеджеров демо-ландшафта
  .\install.ps1 -Action uninstall    остановить (данные остаются); с -Purge — удалить данные и образ

Параметры: -Yes (без вопросов), -WebPort 8081, -DeepseekKeyFrom C:\путь\к\.env

Если PowerShell не даёт запустить скрипт: powershell -ExecutionPolicy Bypass -File .\install.ps1
Кабинет открывается на http://localhost:8081. Вход через Telegram требует публичного адреса с https —
на рабочей станции его нет, поэтому для демо с входом ставьте на сервер (install.sh).
#>
param(
    [ValidateSet("install", "update", "configure", "status", "tokens", "uninstall")]
    [string]$Action = "install",
    [switch]$Yes,
    [switch]$Purge,
    [string]$WebPort = "",
    [string]$DeepseekKeyFrom = ""
)

$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

$Dir = $PSScriptRoot
$Image = "metabrain/app:local"
$Compose = Join-Path $Dir "deploy\compose.yml"
$EnvFile = Join-Path $Dir "deploy\.env"
$Interactive = (-not $Yes) -and [Environment]::UserInteractive

function Say($text) { Write-Host ""; Write-Host "== $text" }
function Fail($text) { Write-Host ""; Write-Host "Ошибка: $text" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    # Нативная команда без превращения stderr в исключение (PowerShell 5.1); код возврата — в $LASTEXITCODE.
    param([string]$File, [string[]]$Arguments, [string]$InputText = $null)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($null -ne $InputText) { $InputText | & $File @Arguments 2>&1 | ForEach-Object { "$_" } }
        else { & $File @Arguments 2>&1 | ForEach-Object { "$_" } }
    } finally { $ErrorActionPreference = $previous }
}

function Compose([string[]]$Arguments) {
    Invoke-Native "docker" (@("compose", "-f", $Compose, "--env-file", $EnvFile) + $Arguments)
}

function Installer([string[]]$Arguments, [string]$InputText = $null) {
    # Модуль установщика в образе MetaBrain: секреты идут через stdin и не печатаются.
    Invoke-Native "docker" (@("run", "--rm", "-i", "--user", "0", "-v", "${Dir}:/w", "-w", "/w", $Image,
        "python", "-m", "src.common.installer") + $Arguments + @("--env", "deploy/.env")) $InputText
}

function Preflight {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "нужен Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
    }
    Invoke-Native "docker" @("compose", "version") | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "нужен Docker Compose v2 (входит в Docker Desktop)" }
    Invoke-Native "docker" @("info") | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop не запущен — запустите его и повторите" }
    $drive = (Get-Item $Dir).PSDrive
    $freeMb = [math]::Floor($drive.Free / 1MB)
    if ($freeMb -lt 2000) { Fail "мало места на диске ${drive}: свободно $freeMb МБ, нужно не меньше 2000 МБ" }
    Write-Host "Docker готов, свободно $freeMb МБ"
}

function Build {
    Say "Сборка образа (несколько минут при первом запуске)"
    $script:OldImage = (Invoke-Native "docker" @("image", "inspect", "-f", "{{.Id}}", $Image)) -join ""
    if ($LASTEXITCODE -ne 0) { $script:OldImage = "" }
    Invoke-Native "docker" @("build", "-q", "-t", $Image, "-f", (Join-Path $Dir "Dockerfile.stub"), $Dir) | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "сборка образа не удалась" }
}

function Read-Secret($prompt) {
    $secure = Read-Host -AsSecureString "$prompt (Enter — оставить как есть)"
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Set-Value($name, $value) {
    if ($value) { Installer @("set", $name) $value | Write-Host }
}

function Configure {
    Say "Конфигурация"
    Installer @("init", "--template", ".env.example") | Write-Host
    if ($DeepseekKeyFrom) {
        if (-not (Test-Path $DeepseekKeyFrom)) { Fail "-DeepseekKeyFrom: нет файла $DeepseekKeyFrom" }
        $source = (Resolve-Path $DeepseekKeyFrom).Path
        Invoke-Native "docker" @("run", "--rm", "--user", "0", "-v", "${Dir}:/w", "-v", "${source}:/import.env:ro", "-w", "/w",
            $Image, "python", "-m", "src.common.installer", "import", "DEEPSEEK_API_KEY", "--from", "/import.env",
            "--env", "deploy/.env") | Write-Host
    }
    if ($WebPort) { Set-Value "WEB_PORT" $WebPort; Set-Value "WEB_BASE_URL" "http://localhost:$WebPort" }
    Installer @("show") | Write-Host
    if ($Interactive) {
        Write-Host ""
        Write-Host "Ключи вводятся скрыто и сохраняются только в $EnvFile."
        Set-Value "DEEPSEEK_API_KEY" (Read-Secret "Ключ DeepSeek API")
        Set-Value "TELEGRAM_BOT_TOKEN" (Read-Secret "Токен ОТДЕЛЬНОГО бота MetaBrain из BotFather")
    }
}

function Wait-Healthy {
    $pending = @()
    for ($i = 0; $i -lt 40; $i++) {
        # Только строки «сервис=состояние»: предупреждения compose из stderr сюда не попадают.
        $pending = @(Compose @("ps", "--format", "{{.Service}}={{.Health}}") |
            Where-Object { ($_ -match "^[a-z-]+=") -and ($_ -notmatch "=healthy$") })
        if ($pending.Count -eq 0) { break }
        Start-Sleep -Seconds 3
    }
    Compose @("ps", "--format", "table {{.Service}}`t{{.Status}}") | Write-Host
    if ($pending.Count -gt 0) { Fail "не поднялись: $($pending -join ', ') — docker compose -f `"$Compose`" logs" }
}

function Up {
    Say "Запуск"
    Compose @("up", "-d", "--remove-orphans") | Write-Host
    Wait-Healthy
    $new = (Invoke-Native "docker" @("image", "inspect", "-f", "{{.Id}}", $Image)) -join ""
    if ($script:OldImage -and $script:OldImage -ne $new) {
        Invoke-Native "docker" @("image", "rm", $script:OldImage) | Out-Null
    }
}

function Checks {
    Say "Проверки"
    Installer @("check-llm") | Write-Host
    Installer @("check-bot") | Write-Host
    Compose @("up", "-d", "harness", "web") | Out-Null
}

function Summary {
    $port = if ($WebPort) { $WebPort } else { "8081" }
    Say "Готово"
    Write-Host "Кабинет: http://localhost:$port"
    Write-Host "Токены менеджеров демо:  .\install.ps1 -Action tokens"
    Write-Host "Поменять ключи:          .\install.ps1 -Action configure"
    if ($Interactive) { Start-Process "http://localhost:$port" }
}

switch ($Action) {
    { $_ -in "install", "update" } { Preflight; Build; Configure; Up; Checks; Summary }
    "configure" { Configure; Compose @("up", "-d") | Write-Host; Wait-Healthy; Checks; Summary }
    "status" { Compose @("ps", "--format", "table {{.Service}}`t{{.Status}}") | Write-Host; Installer @("show") | Write-Host }
    "tokens" { Installer @("tokens") | Write-Host }
    "uninstall" {
        if ($Interactive) {
            $answer = Read-Host "Остановить MetaBrain$(if ($Purge) { ' и удалить данные' })? [y/N]"
            if ($answer -ne "y") { exit 0 }
        }
        if ($Purge) {
            Compose @("down", "-v", "--remove-orphans") | Write-Host
            Invoke-Native "docker" @("image", "rm", $Image) | Out-Null
            Write-Host "Данные и образ удалены; конфигурация осталась в $EnvFile"
        } else {
            Compose @("down", "--remove-orphans") | Write-Host
            Write-Host "MetaBrain остановлен; данные сохранены"
        }
    }
}
