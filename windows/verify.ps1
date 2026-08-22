[CmdletBinding()]
param(
    [switch]$FullFileHash
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script:Failures = 0

function Assert-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )
    if ($Ok) {
        Write-Host ("[OK]   {0}: {1}" -f $Name, $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ("[FAIL] {0}: {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:Failures += 1
    }
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 5
    Assert-Check "后端" ($null -ne $health) "health API 可访问"
}
catch {
    Assert-Check "后端" $false $_.Exception.Message
}

try {
    $front = Invoke-WebRequest -Uri "http://127.0.0.1:5173" -UseBasicParsing -TimeoutSec 5
    Assert-Check "前端" ($front.StatusCode -eq 200) "HTTP $($front.StatusCode)"
}
catch {
    Assert-Check "前端" $false $_.Exception.Message
}

try {
    $tcp = New-Object Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", 8765)
    $tcp.Close()
    Assert-Check "Mineflayer worker" $true "127.0.0.1:8765 可连接"
}
catch {
    Assert-Check "Mineflayer worker" $false $_.Exception.Message
}

$composeFile = Join-Path $Root "docker-compose.yml"
$containerId = (& docker compose -f $composeFile ps -q postgres | Out-String).Trim()
if ($containerId) {
    $counts = (& docker exec $containerId psql -U mc_agent -d mc_agent -At -F "|" -c "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM skills), (SELECT count(*) FROM knowledge_chunks);" 2>&1 | Out-String).Trim()
    Assert-Check "PostgreSQL 数据" ($LASTEXITCODE -eq 0 -and $counts -match '^\d+\|\d+\|\d+$') $(if ($counts) { "runs|skills|knowledge_chunks = $counts" } else { "查询失败" })
}
else {
    Assert-Check "PostgreSQL 容器" $false "未找到容器"
}

$sqliteCount = (Get-ChildItem -LiteralPath (Join-Path $Root "runs") -Filter "*.sqlite3" -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
Assert-Check "SQLite 历史审计" ($sqliteCount -gt 0) "$sqliteCount 个数据库文件"

if ($FullFileHash) {
    $manifest = Join-Path $Root "PACKAGE_MANIFEST.sha256"
    if (-not (Test-Path $manifest)) {
        Assert-Check "文件清单" $false "缺少 PACKAGE_MANIFEST.sha256"
    }
    else {
        $bad = 0
        $checked = 0
        foreach ($line in Get-Content -LiteralPath $manifest) {
            if ($line -notmatch '^([0-9a-f]{64})  (.+)$') {
                continue
            }
            $expected = $Matches[1]
            $relative = $Matches[2].Replace("/", "\")
            $path = Join-Path $Root $relative
            if (-not (Test-Path $path)) {
                $bad += 1
                continue
            }
            $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expected) {
                $bad += 1
            }
            $checked += 1
        }
        Assert-Check "完整文件哈希" ($bad -eq 0) "$checked 个文件已检查，$bad 个异常"
    }
}

if ($script:Failures -gt 0) {
    throw "验证失败：$script:Failures 项。"
}

Write-Host ""
Write-Host "Windows 迁移环境验证通过。" -ForegroundColor Green

