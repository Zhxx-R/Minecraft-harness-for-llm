[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServiceDir = Join-Path $Root "services\mineclip-scorer"
$BackendPython = Join-Path $Root "backend\.venv\Scripts\python.exe"
$MineclipPython = Join-Path $ServiceDir ".venv\Scripts\python.exe"
$Checkpoint = Join-Path $ServiceDir "checkpoints\attn.pth"
$ExpectedMd5 = "b5ece9198337cfd117a3bfbd921e56da"

if (-not (Test-Path $BackendPython)) {
    throw "后端 Python 环境不存在。先运行 windows\setup.ps1。"
}
if (-not (Test-Path $Checkpoint)) {
    throw "迁移包中缺少 MineCLIP checkpoint：$Checkpoint"
}
$actualMd5 = (Get-FileHash -LiteralPath $Checkpoint -Algorithm MD5).Hash.ToLowerInvariant()
if ($actualMd5 -ne $ExpectedMd5) {
    throw "MineCLIP checkpoint MD5 不匹配：$actualMd5"
}
if (-not (Test-Path (Join-Path $ServiceDir "vendor\MineCLIP\mineclip"))) {
    throw "迁移包中缺少 MineCLIP vendor 代码。"
}

if (-not (Test-Path $MineclipPython)) {
    & $BackendPython -m venv (Join-Path $ServiceDir ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "无法创建 MineCLIP 虚拟环境。"
    }
}

& $MineclipPython -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) {
    throw "无法升级 MineCLIP pip。"
}
& $MineclipPython -m pip install -r (Join-Path $ServiceDir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "无法安装 MineCLIP 运行依赖。"
}
& $MineclipPython -m pip install -r (Join-Path $ServiceDir "requirements-setup.txt")
if ($LASTEXITCODE -ne 0) {
    throw "无法安装 MineCLIP 设置依赖。"
}

Write-Host "MineCLIP Windows 环境安装完成。" -ForegroundColor Green

