[CmdletBinding()]
param(
    [switch]$StopInfrastructure
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime\windows"

foreach ($name in @("frontend", "backend", "worker", "mineclip", "minecraft")) {
    $pidPath = Join-Path $RuntimeDir "$name.pid"
    if (-not (Test-Path $pidPath)) {
        continue
    }
    $pidValue = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($process) {
        & taskkill.exe /PID $pidValue /T /F *> $null
        Write-Host "已停止 $name，PID $pidValue。"
    }
    Remove-Item -LiteralPath $pidPath -Force
}

if ($StopInfrastructure) {
    & docker compose -f (Join-Path $Root "docker-compose.yml") down
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose 停止失败。"
    }
}

Write-Host "项目进程已停止。" -ForegroundColor Green

