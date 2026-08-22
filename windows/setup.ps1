[CmdletBinding()]
param(
    [switch]$RestoreDatabase,
    [switch]$AllowDatabaseReset,
    [switch]$SkipInfrastructure,
    [switch]$WithMineclip,
    [switch]$FullTests
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvPath = Join-Path $Root ".env"
$EnvTemplate = Join-Path $PSScriptRoot ".env.windows.example"
$BackendVenvPython = Join-Path $Root "backend\.venv\Scripts\python.exe"

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label 失败，退出码 $LASTEXITCODE。"
    }
}

function New-HexSecret {
    param([int]$Bytes = 24)
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    }
    finally {
        $generator.Dispose()
    }
    return -join ($buffer | ForEach-Object { $_.ToString("x2") })
}

function Find-Python311 {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3.11 -c "import sys; raise SystemExit(sys.version_info < (3,11) or sys.version_info >= (3,12))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $py.Source; Prefix = @("-3.11") }
        }
    }
    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        & $command.Source -c "import sys; raise SystemExit(sys.version_info < (3,11) or sys.version_info >= (3,12))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ File = $command.Source; Prefix = @() }
        }
    }
    throw "需要 Python 3.11 x64。"
}

Push-Location $Root
try {
    & (Join-Path $PSScriptRoot "check-prerequisites.ps1")

    if (-not (Test-Path $EnvPath)) {
        $postgresPassword = New-HexSecret
        $rconPassword = New-HexSecret
        $envText = [IO.File]::ReadAllText($EnvTemplate)
        $envText = $envText.Replace("__POSTGRES_PASSWORD__", $postgresPassword)
        $envText = $envText.Replace("__RCON_PASSWORD__", $rconPassword)
        [IO.File]::WriteAllText(
            $EnvPath,
            $envText,
            (New-Object Text.UTF8Encoding($false))
        )
        Write-Host "已生成新的 .env；QWEN_API_KEY 仍为 replace-me。" -ForegroundColor Green
    }
    else {
        Write-Host "保留现有 .env，不覆盖本机配置。"
    }

    if (-not (Test-Path $BackendVenvPython)) {
        $python = Find-Python311
        $venvArgs = @($python.Prefix) + @("-m", "venv", (Join-Path $Root "backend\.venv"))
        Invoke-Checked "创建 Python 3.11 虚拟环境" {
            & $python.File @venvArgs
        }
    }

    Invoke-Checked "升级 Python 打包工具" {
        & $BackendVenvPython -m pip install --upgrade pip wheel
    }
    Invoke-Checked "安装后端依赖" {
        & $BackendVenvPython -m pip install -c (Join-Path $Root "backend\constraints-handoff.txt") -e "$Root\backend[dev]"
    }

    Invoke-Checked "安装 Mineflayer worker 依赖" {
        Push-Location (Join-Path $Root "workers\mineflayer-worker")
        try { & npm.cmd ci } finally { Pop-Location }
    }
    Invoke-Checked "安装前端依赖" {
        Push-Location (Join-Path $Root "frontend")
        try { & npm.cmd ci } finally { Pop-Location }
    }

    if (-not $SkipInfrastructure) {
        Invoke-Checked "启动 PostgreSQL/pgvector 与 Redis" {
            & docker compose -f (Join-Path $Root "docker-compose.yml") up -d postgres redis
        }

        if ($RestoreDatabase) {
            if (-not $AllowDatabaseReset) {
                throw "-RestoreDatabase 必须同时显式提供 -AllowDatabaseReset。"
            }
            & (Join-Path $PSScriptRoot "restore-database.ps1") -AllowDatabaseReset
        }

        $oldPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = Join-Path $Root "backend\src"
        try {
            Invoke-Checked "执行数据库迁移" {
                & $BackendVenvPython -m alembic -c (Join-Path $Root "alembic.ini") upgrade head
            }
        }
        finally {
            $env:PYTHONPATH = $oldPythonPath
        }
    }

    if ($WithMineclip) {
        & (Join-Path $PSScriptRoot "setup-mineclip.ps1")
    }

    Invoke-Checked "验证共享 JSON Schema" {
        & $BackendVenvPython (Join-Path $Root "scripts\validate_json_schemas.py")
    }
    Invoke-Checked "编译检查后端" {
        & $BackendVenvPython -m compileall -q (Join-Path $Root "backend\src")
    }
    Invoke-Checked "检查 worker 类型" {
        Push-Location (Join-Path $Root "workers\mineflayer-worker")
        try { & npm.cmd run typecheck } finally { Pop-Location }
    }
    Invoke-Checked "检查前端类型" {
        Push-Location (Join-Path $Root "frontend")
        try { & npm.cmd run typecheck } finally { Pop-Location }
    }

    if ($FullTests) {
        Invoke-Checked "运行后端测试" {
            Push-Location (Join-Path $Root "backend")
            try { & $BackendVenvPython -m pytest } finally { Pop-Location }
        }
    }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Windows 初始化完成。" -ForegroundColor Green
Write-Host "下一步：.\windows\start-all.ps1"

