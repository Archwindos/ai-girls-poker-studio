$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$uiRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'webui')).Path
$stateRoot = Join-Path $projectRoot '05-制作记录\当前状态'
$server = Join-Path $uiRoot 'serve_review.py'
$candidates = @((Join-Path $projectRoot '.venv\Scripts\python.exe'), (Join-Path $projectRoot 'venv\Scripts\python.exe'))
if ($env:VIRTUAL_ENV) { $candidates += Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe' }
$command = Get-Command python.exe -ErrorAction SilentlyContinue
if ($command -and $command.Source -notlike '*\Microsoft\WindowsApps\*') { $candidates += $command.Source }
$candidates += Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$python = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
        & $candidate -c 'import PIL, pypdf, reportlab, pypdfium2, numpy' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { continue }
}
if (-not $python) { throw '未找到含完整依赖的 Python。请按工具说明安装 requirements.txt。' }
$fingerprint = (& $python -B $server --fingerprint).Trim()
if ($LASTEXITCODE -ne 0 -or $fingerprint -notmatch '^[0-9a-f]{20}$') { throw '无法计算工具版本，请检查 Python 环境。' }
function Get-DeckHealth([int]$port) {
    try { return Invoke-RestMethod -Uri ("http://127.0.0.1:$port/api/health") -TimeoutSec 2 }
    catch { return $null }
}
function Test-Current($health) {
    return $null -ne $health -and $health.app -eq 'ai-girls-deck' -and $health.api_version -ge 3 -and
        $health.code_fingerprint -eq $fingerprint -and $health.active_writer -eq $true -and
        [string]::Equals($health.state_root, $stateRoot, [StringComparison]::OrdinalIgnoreCase)
}
function Open-Current([int]$port, $health) {
    $info = [ordered]@{ port=$port; url="http://127.0.0.1:$port"; code_fingerprint=$fingerprint; state_root=$stateRoot }
    $info | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'last-runtime.json') -Encoding UTF8
    Start-Process $info.url
}
$activeFile = Join-Path $stateRoot 'active-runtime.json'
if (Test-Path -LiteralPath $activeFile) {
    try {
        $active = Get-Content -LiteralPath $activeFile -Raw | ConvertFrom-Json
        $health = Get-DeckHealth $active.port
        if (Test-Current $health) { Open-Current $active.port $health; exit 0 }
    } catch {}
}
$selectedPort = $null
foreach ($port in 8765..8785) {
    $client = New-Object System.Net.Sockets.TcpClient
    $occupied = $false
    try { $occupied = $client.ConnectAsync('127.0.0.1', $port).Wait(200) -and $client.Connected }
    catch {} finally { $client.Dispose() }
    if (-not $occupied) { $selectedPort = $port; break }
    $health = Get-DeckHealth $port
    if (Test-Current $health) { Open-Current $port $health; exit 0 }
    if ($health -and $health.app -eq 'ai-girls-deck') {
        Write-Host "端口 $port 上的旧版页面已退休；其旧状态不会进入新工具，将打开当前版本。"
    }
}
if ($null -eq $selectedPort) { throw '8765–8785 端口均已占用，请检查本地服务。启动器不会关闭任何进程。' }
$arguments = '-B "{0}" --root "{1}" --state-root "{2}" --port {3}' -f $server, $uiRoot, $stateRoot, $selectedPort
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $uiRoot "server-$selectedPort.stdout.log") -RedirectStandardError (Join-Path $uiRoot "server-$selectedPort.stderr.log")
for ($attempt=0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 250
    $health = Get-DeckHealth $selectedPort
    if (Test-Current $health) {
        Set-Content -LiteralPath (Join-Path $uiRoot 'server.pid') -Value $process.Id -Encoding ASCII
        Open-Current $selectedPort $health
        exit 0
    }
    if ($process.HasExited) { break }
}
if (Test-Path -LiteralPath $activeFile) {
    $active = Get-Content -LiteralPath $activeFile -Raw | ConvertFrom-Json
    $health = Get-DeckHealth $active.port
    if (Test-Current $health) { Open-Current $active.port $health; exit 0 }
}
throw "服务未能启动。请查看 webui/server-$selectedPort.stderr.log。"
