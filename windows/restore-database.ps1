[CmdletBinding()]
param(
    [string]$DumpPath = "",
    [switch]$AllowDatabaseReset
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $Root "docker-compose.yml"

if (-not $DumpPath) {
    $DumpPath = Join-Path $Root "database\postgres\mc_agent.dump"
}
$DumpPath = (Resolve-Path $DumpPath).Path

if (-not $AllowDatabaseReset) {
    throw "恢复会删除并重建目标 mc_agent 数据库。确认目标可覆盖后，重新运行并添加 -AllowDatabaseReset。"
}

Write-Host "启动 PostgreSQL 容器..."
& docker compose -f $ComposeFile up -d postgres
if ($LASTEXITCODE -ne 0) {
    throw "无法启动 PostgreSQL。"
}

$containerId = (& docker compose -f $ComposeFile ps -q postgres | Out-String).Trim()
if (-not $containerId) {
    throw "未找到这份项目的 PostgreSQL 容器。"
}

$ready = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    & docker exec $containerId pg_isready -U mc_agent -d postgres *> $null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    throw "PostgreSQL 在 120 秒内未就绪。"
}

$containerDump = "/tmp/mc_agent_windows_transfer.dump"
Write-Host "复制并校验 PostgreSQL custom-format 备份..."
& docker cp $DumpPath "${containerId}:$containerDump"
if ($LASTEXITCODE -ne 0) {
    throw "无法把备份复制到 PostgreSQL 容器。"
}
& docker exec $containerId pg_restore --list $containerDump *> $null
if ($LASTEXITCODE -ne 0) {
    throw "pg_restore 无法读取备份目录，备份可能损坏或版本不兼容。"
}

Write-Host "重建 mc_agent 数据库并恢复数据..."
$terminateSql = @"
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'mc_agent' AND pid <> pg_backend_pid();
"@

& docker exec $containerId psql -v ON_ERROR_STOP=1 -U mc_agent -d postgres -c $terminateSql
if ($LASTEXITCODE -ne 0) {
    throw "无法终止 mc_agent 数据库连接。"
}

& docker exec $containerId psql -v ON_ERROR_STOP=1 -U mc_agent -d postgres -c "DROP DATABASE IF EXISTS mc_agent;"
if ($LASTEXITCODE -ne 0) {
    throw "无法删除 mc_agent 数据库。"
}

& docker exec $containerId psql -v ON_ERROR_STOP=1 -U mc_agent -d postgres -c "CREATE DATABASE mc_agent OWNER mc_agent;"
if ($LASTEXITCODE -ne 0) {
    throw "无法重建 mc_agent 数据库。"
}

& docker exec $containerId pg_restore --exit-on-error --no-owner --no-privileges -U mc_agent -d mc_agent $containerDump
if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL 数据恢复失败。目标数据库可能处于部分恢复状态。"
}

& docker exec $containerId rm -f $containerDump
Write-Host "PostgreSQL 数据恢复完成。" -ForegroundColor Green

