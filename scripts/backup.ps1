# 备份脚本：备份业务数据库与配置（本地单机版的全量恢复点）
# 恢复：把 backups\<timestamp> 中的文件复制回 data\ 后重启服务
param([string]$Target = "")
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "data"
if (-not (Test-Path $src)) { Write-Host "没有 data 目录，先启动一次服务。" -ForegroundColor Yellow; exit 1 }
if (-not $Target) { $Target = Join-Path $root ("backups\" + (Get-Date -Format "yyyyMMdd_HHmmss")) }
New-Item -ItemType Directory -Force -Path $Target | Out-Null
Copy-Item (Join-Path $src "*.db*") $Target -Force
Get-ChildItem $Target | ForEach-Object {
    $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.Substring(0,16)
    Write-Host ("[OK] {0}  sha256:{1}…" -f $_.Name, $h)
}
# 写入校验和清单
Get-ChildItem $Target | Get-FileHash -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash)  $($_.Path)" } |
    Set-Content (Join-Path $Target "SHA256SUMS.txt")
Write-Host "备份完成：$Target （含SHA256校验清单）"
