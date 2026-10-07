# Paketlenmiş sbs.exe'nin gerçekten çalıştığını Windows'ta uçtan uca doğrular.
#   pwsh -File packaging\smoke-test.ps1 -Exe dist\sbs.exe
# Kendi ayarlarınıza dokunmaz: APPDATA geçici bir dizine yönlendirilir.
param(
    [string]$Exe = "dist\sbs.exe",
    [switch]$SkipService
)
$ErrorActionPreference = "Stop"
$Exe = (Resolve-Path $Exe).Path

$work = Join-Path ([System.IO.Path]::GetTempPath()) "sbs-smoke-$PID"
New-Item -ItemType Directory -Force $work | Out-Null
$env:APPDATA = Join-Path $work "appdata"
$vault = Join-Path $work "SBS"
$proj = Join-Path $work "Proje Ödev"          # boşluk ve Türkçe karakter bilerek
New-Item -ItemType Directory -Force (Join-Path $proj "src") | Out-Null
[IO.File]::WriteAllText((Join-Path $proj "src\main.py"), "print('v1')`n")
[IO.File]::WriteAllText((Join-Path $proj "README.md"), "# proje`n")

function Step([string]$Name) { Write-Host "`n=== $Name" -ForegroundColor Cyan }
function Sbs {
    & $Exe @args
    if ($LASTEXITCODE -ne 0) { throw "sbs $($args -join ' ') başarısız (çıkış $LASTEXITCODE)" }
}

try {
    Step "sürüm ve yardım"
    Sbs --version
    Sbs --help | Out-Null

    Step "vault + hedef + ilk yedek"
    Sbs init $vault
    Sbs add $proj odev --exclude node_modules --max-backups 5

    Step "değişiklik → kontrol → geri yükleme"
    [IO.File]::WriteAllText((Join-Path $proj "src\main.py"), "print('bozuk')`n")
    [IO.File]::WriteAllText((Join-Path $proj "fazla.txt"), "x")
    Set-ItemProperty (Join-Path $proj "fazla.txt") -Name IsReadOnly -Value $true
    Sbs status odev
    Sbs check odev
    Sbs history odev
    Sbs restore odev 1 -y
    $content = [IO.File]::ReadAllText((Join-Path $proj "src\main.py"))
    if ($content -ne "print('v1')`n") { throw "geri yükleme içeriği yanlış: $content" }
    if (Test-Path (Join-Path $proj "fazla.txt")) { throw "salt-okunur fazla dosya silinmedi" }

    Step "hedef dizinin içindeyken isimsiz komut"
    Push-Location $proj
    try { Sbs status } finally { Pop-Location }

    Step "istatistik, günlük, tamamlama"
    Sbs stats odev
    Sbs log
    Sbs completion powershell | Out-Null

    Step "TUI (ekransız duman testi: tüm pencereler açılıp kapanır)"
    $env:SBS_TUI_SMOKE = "1"
    try { Sbs tui } finally { Remove-Item Env:SBS_TUI_SMOKE }

    Step "pencere açmadan izleyici (conhost --headless)"
    [IO.File]::WriteAllText((Join-Path $proj "yeni.txt"), "x")
    $log = Join-Path $work "watch-once.log"
    $conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
    $argLine = "--headless `"$Exe`" --vault `"$vault`" watch --once --log `"$log`""
    # watch --once yalnızca zamanı gelen hedefleri kontrol eder; aralığı kısaltıp bekle
    Sbs config odev --interval 1s
    Start-Sleep -Seconds 2
    Start-Process -FilePath $conhost -ArgumentList $argLine -Wait -WindowStyle Hidden
    if (-not (Test-Path $log)) { throw "conhost --headless ile izleyici günlük yazmadı" }
    Get-Content $log
    # "0 yedek alındı" de eşleşmesin diye yedeğin kendi satırı aranır: "[odev] yedek #3: ..."
    if (-not (Select-String -Path $log -Pattern "\[odev\] yedek #\d+:" -Quiet)) { throw "izleyici yedek almadı" }

    if (-not $SkipService) {
        Step "Görev Zamanlayıcı servisi"
        Sbs config odev --interval 7m
        Sbs service install
        $hb = Join-Path $vault ".locks\watch.json"
        for ($i = 0; $i -lt 45 -and -not (Test-Path $hb); $i++) { Start-Sleep -Seconds 1 }
        Sbs service status
        if (-not (Test-Path $hb)) { throw "servis başlamadı (kalp atışı yok)" }
        $watchPid = (Get-Content $hb | ConvertFrom-Json).pid
        Sbs service uninstall
        Start-Sleep -Seconds 2
        if (Get-Process -Id $watchPid -ErrorAction SilentlyContinue) { throw "izleyici süreci kapanmadı" }
        & schtasks /Query /TN StepByStep 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { throw "görev silinmedi" }
    }

    Write-Host "`nTÜM DUMAN TESTLERİ GEÇTİ" -ForegroundColor Green
}
finally {
    if (-not $SkipService) { & $Exe service uninstall 2>$null | Out-Null }
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
