[CmdletBinding()]
param(
    [switch]$AcceptEula
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ServerDir = Join-Path $Root "infra\minecraft-server"
$RuntimeDir = Join-Path $Root ".runtime\windows"
$PidFile = Join-Path $RuntimeDir "minecraft.pid"
$LogFile = Join-Path $RuntimeDir "minecraft.log"
$EulaFile = Join-Path $ServerDir "eula.txt"
$PropertiesFile = Join-Path $ServerDir "server.properties"

function Read-DotEnv {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $values[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $values
}

if (-not (Test-Path $ServerDir)) {
    throw "缺少 Minecraft server 目录：$ServerDir"
}

if ($AcceptEula) {
    [IO.File]::WriteAllText(
        $EulaFile,
        "eula=true`r`n",
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host "已在本机记录 Minecraft EULA 接受状态。"
}

if (-not (Test-Path $EulaFile) -or -not (Select-String -LiteralPath $EulaFile -Pattern '^eula=true$' -Quiet)) {
    throw "请先阅读 https://aka.ms/MinecraftEULA。明确接受后，重新运行并添加 -AcceptEula。"
}

$envValues = Read-DotEnv (Join-Path $Root ".env")
$rconPassword = $envValues["MINECRAFT_RCON_PASSWORD"]
$serverPort = $envValues["MINECRAFT_PORT"]
$rconPort = $envValues["MINECRAFT_RCON_PORT"]
$xms = $envValues["MC_SERVER_XMS"]
$xmx = $envValues["MC_SERVER_XMX"]
if (-not $serverPort) { $serverPort = "25565" }
if (-not $rconPort) { $rconPort = "25575" }
if (-not $xms) { $xms = "1G" }
if (-not $xmx) { $xmx = "3G" }
if (-not $rconPassword -or $rconPassword -eq "replace-me" -or $rconPassword.Length -lt 8) {
    throw ".env 中缺少有效的 MINECRAFT_RCON_PASSWORD。先运行 windows\setup.ps1。"
}

$template = [IO.File]::ReadAllText((Join-Path $Root "configs\minecraft\server.properties.template"))
$properties = $template.Replace("__SERVER_PORT__", $serverPort)
$properties = $properties.Replace("__RCON_PORT__", $rconPort)
$properties = $properties.Replace("__RCON_PASSWORD__", $rconPassword)
[IO.File]::WriteAllText(
    $PropertiesFile,
    $properties,
    (New-Object Text.UTF8Encoding($false))
)

$fabricJar = Join-Path $ServerDir "fabric-server-launch.jar"
$vanillaJar = Join-Path $ServerDir "server-1.20.1.jar"
$serverJar = if (Test-Path $fabricJar) { $fabricJar } else { $vanillaJar }
if (-not (Test-Path $serverJar)) {
    throw "缺少 Minecraft 1.20.1 server jar。"
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (Test-Path $PidFile) {
    $existingPid = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Host "Minecraft 已在运行，PID $existingPid。"
        exit 0
    }
    Remove-Item -LiteralPath $PidFile -Force
}

$process = Start-Process `
    -FilePath "java.exe" `
    -ArgumentList @("-Xms$xms", "-Xmx$xmx", "-jar", $serverJar, "nogui") `
    -WorkingDirectory $ServerDir `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError (Join-Path $RuntimeDir "minecraft.error.log") `
    -WindowStyle Hidden `
    -PassThru

[IO.File]::WriteAllText($PidFile, "$($process.Id)`r`n")
Write-Host "Minecraft Server 已启动，PID $($process.Id)，日志：$LogFile" -ForegroundColor Green

