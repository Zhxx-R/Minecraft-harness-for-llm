[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BaseArchiveName
)

$ErrorActionPreference = "Stop"
$target = [IO.Path]::GetFullPath($BaseArchiveName)
$directory = [IO.Path]::GetDirectoryName($target)
$leaf = [IO.Path]::GetFileName($target)
$parts = Get-ChildItem -LiteralPath $directory -Filter "$leaf.part*" -File |
    Sort-Object Name
if (-not $parts) {
    throw "未找到分卷：$target.part001"
}
if (Test-Path $target) {
    throw "目标 ZIP 已存在，为避免覆盖已停止：$target"
}

$output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
try {
    foreach ($part in $parts) {
        $checksumPath = "$($part.FullName).sha256"
        if (-not (Test-Path $checksumPath)) {
            throw "缺少分卷校验文件：$checksumPath"
        }
        $line = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+') {
            throw "分卷校验文件格式错误：$checksumPath"
        }
        $expected = $Matches[1].ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath $part.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "分卷校验失败：$($part.Name)"
        }
        $input = [IO.File]::OpenRead($part.FullName)
        try {
            $input.CopyTo($output)
        }
        finally {
            $input.Dispose()
        }
        Write-Host "已合并 $($part.Name)"
    }
}
finally {
    $output.Dispose()
}

Write-Host "ZIP 已重组：$target" -ForegroundColor Green

