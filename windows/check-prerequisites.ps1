[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$script:Failures = 0

function Write-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail
    )

    if ($Ok) {
        Write-Host ("[OK]   {0}: {1}" -f $Name, $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ("[MISS] {0}: {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:Failures += 1
    }
}

function Find-Python311 {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $version = & $py.Source -3.11 -c "import platform; print(platform.python_version())" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                return @{ File = $py.Source; Prefix = @("-3.11"); Version = $version.Trim() }
            }
        }
        catch {
        }
    }

    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        try {
            $version = & $command.Source -c "import platform,sys; print(platform.python_version()); raise SystemExit(sys.version_info < (3,11) or sys.version_info >= (3,12))" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return @{ File = $command.Source; Prefix = @(); Version = $version.Trim() }
            }
        }
        catch {
        }
    }
    return $null
}

$python = Find-Python311
Write-Check "Python 3.11" ($null -ne $python) $(if ($python) { $python.Version } else { "安装 Python 3.11 x64，并启用 py launcher/PATH" })

$node = Get-Command node -ErrorAction SilentlyContinue
$nodeVersion = if ($node) { (& $node.Source --version).Trim() } else { "" }
$nodeMajor = if ($nodeVersion -match '^v(\d+)') { [int]$Matches[1] } else { 0 }
Write-Check "Node.js" ($nodeMajor -ge 20) $(if ($node) { $nodeVersion } else { "安装 Node.js 22 LTS" })

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
}
Write-Check "npm" ($null -ne $npm) $(if ($npm) { (& $npm.Source --version).Trim() } else { "随 Node.js 安装" })

$java = Get-Command java -ErrorAction SilentlyContinue
$javaText = ""
$javaMajor = 0
if ($java) {
    $javaText = (& cmd.exe /c "`"$($java.Source)`" -version 2>&1" | Select-Object -First 1).ToString()
    if ($javaText -match '"(\d+)') {
        $javaMajor = [int]$Matches[1]
    }
}
Write-Check "Java" ($javaMajor -ge 17) $(if ($java) { $javaText } else { "安装 Java 17 x64" })

$docker = Get-Command docker -ErrorAction SilentlyContinue
Write-Check "Docker CLI" ($null -ne $docker) $(if ($docker) { (& $docker.Source --version).Trim() } else { "安装 Docker Desktop" })

if ($docker) {
    $composeText = (& cmd.exe /c "`"$($docker.Source)`" compose version 2>&1" | Out-String).Trim()
    Write-Check "Docker Compose" ($LASTEXITCODE -eq 0) $(if ($composeText) { $composeText } else { "Docker Desktop 应自带 Compose v2" })

    $dockerInfo = (& cmd.exe /c "`"$($docker.Source)`" info --format `"{{.ServerVersion}}`" 2>&1" | Out-String).Trim()
    Write-Check "Docker Engine" ($LASTEXITCODE -eq 0) $(if ($LASTEXITCODE -eq 0) { "Server $dockerInfo" } else { "请启动 Docker Desktop，并等待状态变为 Running" })
}

try {
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($PSScriptRoot).Substring(0, 1)) -ErrorAction Stop
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Write-Check "可用磁盘" ($freeGb -ge 15) "$freeGb GB（建议至少 15GB）"
}
catch {
    Write-Host "[INFO] 无法读取可用磁盘空间。"
}

if ($script:Failures -gt 0) {
    throw "环境检查发现 $script:Failures 个缺项。安装或启动相应组件后重新运行。"
}

Write-Host ""
Write-Host "环境检查通过。" -ForegroundColor Green

