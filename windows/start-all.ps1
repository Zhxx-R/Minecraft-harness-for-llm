[CmdletBinding()]
param(
    [switch]$WithMineclip,
    [switch]$WithMinecraft
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime\windows"
$BackendPython = Join-Path $Root "backend\.venv\Scripts\python.exe"
$MineclipPython = Join-Path $Root "services\mineclip-scorer\.venv\Scripts\python.exe"
$ComposeFile = Join-Path $Root "docker-compose.yml"

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{}
    )

    $pidPath = Join-Path $RuntimeDir "$Name.pid"
    if (Test-Path $pidPath) {
        $oldPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
        if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
            Write-Host "$Name 已在运行，PID $oldPid。"
            return
        }
        Remove-Item -LiteralPath $pidPath -Force
    }

    foreach ($key in $Environment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $Environment[$key], "Process")
    }
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput (Join-Path $RuntimeDir "$Name.log") `
        -RedirectStandardError (Join-Path $RuntimeDir "$Name.error.log") `
        -WindowStyle Hidden `
        -PassThru
    [IO.File]::WriteAllText($pidPath, "$($process.Id)`r`n")
    Write-Host "$Name 已启动，PID $($process.Id)。"
}

function Wait-Http {
    param(
        [string]$Name,
        [string]$Url,
        [int]$Attempts = 60
    )
    for ($index = 1; $index -le $Attempts; $index++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Host "$Name 已就绪：$Url" -ForegroundColor Green
                return
            }
        }
        catch {
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name 在等待时间内未就绪。查看 .runtime\windows 中的日志。"
}

if (-not (Test-Path $BackendPython)) {
    throw "后端环境不存在。先运行 windows\setup.ps1。"
}
if (-not (Test-Path (Join-Path $Root "workers\mineflayer-worker\node_modules"))) {
    throw "worker 依赖不存在。先运行 windows\setup.ps1。"
}
if (-not (Test-Path (Join-Path $Root "frontend\node_modules"))) {
    throw "前端依赖不存在。先运行 windows\setup.ps1。"
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
& docker compose -f $ComposeFile up -d postgres redis
if ($LASTEXITCODE -ne 0) {
    throw "无法启动 PostgreSQL/Redis。"
}

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $Root "backend\src"
try {
    & $BackendPython -m alembic -c (Join-Path $Root "alembic.ini") upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "数据库迁移失败。"
    }
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}

Start-ManagedProcess `
    -Name "backend" `
    -FilePath $BackendPython `
    -Arguments @("-m", "uvicorn", "mc_agent_harness.main:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory (Join-Path $Root "backend")

Start-ManagedProcess `
    -Name "worker" `
    -FilePath "npm.cmd" `
    -Arguments @("run", "dev") `
    -WorkingDirectory (Join-Path $Root "workers\mineflayer-worker") `
    -Environment @{ "MINEFLAYER_WORKER_PORT" = "8765" }

Start-ManagedProcess `
    -Name "frontend" `
    -FilePath "npm.cmd" `
    -Arguments @("run", "dev", "--", "--host", "127.0.0.1") `
    -WorkingDirectory (Join-Path $Root "frontend") `
    -Environment @{ "VITE_API_BASE_URL" = "http://127.0.0.1:8000" }

if ($WithMineclip) {
    if (-not (Test-Path $MineclipPython)) {
        throw "MineCLIP 环境不存在。先运行 windows\setup-mineclip.ps1。"
    }
    $mineclipService = Join-Path $Root "services\mineclip-scorer"
    Start-ManagedProcess `
        -Name "mineclip" `
        -FilePath $MineclipPython `
        -Arguments @("-m", "uvicorn", "app:app", "--app-dir", $mineclipService, "--host", "127.0.0.1", "--port", "8091") `
        -WorkingDirectory $mineclipService `
        -Environment @{
            "MINECLIP_VARIANT" = "attn"
            "MINECLIP_CHECKPOINT" = (Join-Path $mineclipService "checkpoints\attn.pth")
            "MINECLIP_REPOSITORY" = (Join-Path $mineclipService "vendor\MineCLIP")
            "MINECLIP_DEVICE" = "auto"
            "HF_HOME" = (Join-Path $mineclipService "cache\huggingface")
        }
}

if ($WithMinecraft) {
    & (Join-Path $PSScriptRoot "start-minecraft.ps1")
}

Wait-Http "后端" "http://127.0.0.1:8000/api/health"
Wait-Http "前端" "http://127.0.0.1:5173"
if ($WithMineclip) {
    Wait-Http "MineCLIP" "http://127.0.0.1:8091/health" 120
}

Write-Host ""
Write-Host "项目已启动：http://127.0.0.1:5173" -ForegroundColor Green

