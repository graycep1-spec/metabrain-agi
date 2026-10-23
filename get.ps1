<#
Загрузчик MetaBrain для Windows (PowerShell 5.1+): скачать релиз → сверить SHA-256 → распаковать → установщик.
Со страницы лендинга:  irm https://<страница>/get.ps1 | iex
Явно:  $env:METABRAIN_BASE='https://<страница>'; irm https://<страница>/get.ps1 | iex

Ничего не ставит молча: проверяет контрольную сумму архива, затем запускает установщик (если это консоль)
или печатает команду запуска. Ключи спросит установщик.
#>
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# BASE подставляет процесс сборки релиза; до публикации — $env:METABRAIN_BASE (OPEN-18).
$BaseDefault = "__METABRAIN_BASE__"
$Base = if ($env:METABRAIN_BASE) { $env:METABRAIN_BASE } else { $BaseDefault }
$Version = if ($env:METABRAIN_VERSION) { $env:METABRAIN_VERSION } else { "latest" }

function Fail($m) { Write-Host ""; Write-Host "Ошибка: $m" -ForegroundColor Red; exit 1 }
if ($Base -notmatch '^https://') {
    Fail "адрес дистрибутивов не задан — задайте `$env:METABRAIN_BASE='https://<страница>'` (лендинг ещё не опубликован, OPEN-18)"
}

if ($Version -eq "latest") {
    try { $Version = (Invoke-RestMethod "$Base/latest.txt").Trim() } catch { Fail "не удалось узнать версию ($Base/latest.txt)" }
    if (-not $Version) { Fail "пустой latest.txt" }
}

$Archive = "metabrain-$Version.zip"
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("metabrain-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
    Write-Host "Скачиваю $Archive …"
    Invoke-WebRequest "$Base/$Archive" -OutFile (Join-Path $Tmp $Archive) -UseBasicParsing
    Invoke-WebRequest "$Base/SHA256SUMS" -OutFile (Join-Path $Tmp "SHA256SUMS") -UseBasicParsing

    $expected = $null
    foreach ($line in Get-Content (Join-Path $Tmp "SHA256SUMS")) {
        $parts = $line -split '\s+', 2
        if ($parts.Count -eq 2 -and ($parts[1].TrimStart('*') -eq $Archive)) { $expected = $parts[0].ToLower() }
    }
    if (-not $expected) { Fail "в SHA256SUMS нет строки для $Archive" }
    $actual = (Get-FileHash (Join-Path $Tmp $Archive) -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { Fail "контрольная сумма не совпала — архив повреждён или подменён, установку прервал" }
    Write-Host "SHA-256 совпал."

    $dest = if ($env:METABRAIN_DIR) { $env:METABRAIN_DIR } else { Join-Path $Tmp "metabrain-$Version" }
    Expand-Archive -Path (Join-Path $Tmp $Archive) -DestinationPath $dest -Force
    $installer = Get-ChildItem -Path $dest -Recurse -Filter install.ps1 | Select-Object -First 1
    if (-not $installer) { Fail "в архиве нет install.ps1" }

    if ([Environment]::UserInteractive) {
        Write-Host "Запускаю установщик…"
        & powershell -ExecutionPolicy Bypass -File $installer.FullName
    } else {
        Write-Host ""
        Write-Host "Скачано и проверено: $($installer.Directory.FullName)"
        Write-Host "Запустите:  powershell -ExecutionPolicy Bypass -File `"$($installer.FullName)`""
    }
} finally {
    if (-not $env:METABRAIN_DIR -and -not [Environment]::UserInteractive) {
        # при неинтерактивном запуске распакованное оставляем для ручного запуска, временный архив убираем
        Remove-Item (Join-Path $Tmp $Archive) -ErrorAction SilentlyContinue
    }
}
