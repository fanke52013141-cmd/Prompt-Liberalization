# 提示词优化实验室 · Windows 本机启动脚本
# 用法： powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Port 8620]
param(
    [int]$Port = 8620
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "== 提示词优化实验室 启动诊断 ==" -ForegroundColor Cyan

# 1) Python 检查
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Write-Host "[X] 未找到 python，请先安装 Python 3.11+ 并加入 PATH" -ForegroundColor Red; exit 1 }
$ver = (& python --version) 2>&1
Write-Host "[OK] Python: $ver"

# 2) 依赖检查（fastapi/uvicorn）
$missing = & python -c "import importlib.util,sys; ms=[m for m in ('fastapi','uvicorn') if importlib.util.find_spec(m) is None]; print(' '.join(ms))" 2>$null
if ($missing) {
    Write-Host "[..] 缺少依赖：$missing，正在安装 requirements.txt ..."
    & python -m pip install -r (Join-Path $root "requirements.txt") --quiet
}

# 3) 端口检查
$busy = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($busy) { Write-Host "[X] 端口 $Port 已被占用，换一个端口： -Port 9000" -ForegroundColor Red; exit 1 }
Write-Host "[OK] 端口 $Port 可用"

# 4) 后台启动（不弹多个终端）
$log = Join-Path $root "data\server.log"
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
Start-Process -WindowStyle Hidden -FilePath "python" `
    -ArgumentList "`"$root\run_server.py`" --host 127.0.0.1 --port $Port" `
    -RedirectStandardOutput $log -RedirectStandardError (Join-Path $root "data\server.err.log")

Start-Sleep -Seconds 3
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/workflow-api/v1/templates" -UseBasicParsing -TimeoutSec 5
    Write-Host "[OK] 服务已启动： http://127.0.0.1:$Port  （仅本机回环可访问）" -ForegroundColor Green
} catch {
    Write-Host "[X] 启动失败，请查看 $log 与 data\server.err.log" -ForegroundColor Red
    exit 1
}
