[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Archive,
    [string]$Destination = "C:\mc-agent-harness"
)

$ErrorActionPreference = "Stop"
$Archive = (Resolve-Path $Archive).Path
$checksumPath = "$Archive.sha256"
if (-not (Test-Path $checksumPath)) {
    throw "缺少校验文件：$checksumPath"
}

$checksumLine = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
if ($checksumLine -notmatch '^([0-9a-fA-F]{64})\s+') {
    throw "校验文件格式不正确。"
}
$expected = $Matches[1].ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) {
    throw "ZIP SHA-256 不匹配。Expected=$expected Actual=$actual"
}
Write-Host "ZIP SHA-256 校验通过。" -ForegroundColor Green

if (Test-Path $Destination) {
    $existing = Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($existing) {
        throw "目标目录不是空目录：$Destination"
    }
}
else {
    New-Item -ItemType Directory -Path $Destination | Out-Null
}

Expand-Archive -LiteralPath $Archive -DestinationPath $Destination
Write-Host "解压完成：$Destination" -ForegroundColor Green
Write-Host "进入唯一的顶层目录后，阅读 windows\README_FIRST.zh-CN.md。"

