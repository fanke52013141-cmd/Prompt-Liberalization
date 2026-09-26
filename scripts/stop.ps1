# 停止服务：只停进程，不删除任何数据/数据库文件（TC054：停止不删卷）
$root = Split-Path -Parent $PSScriptRoot
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*run_server.py*" }
if ($procs) {
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force
        Write-Host "[OK] 已停止进程 $($p.ProcessId)"
    }
} else {
    Write-Host "没有正在运行的服务进程。"
}
Write-Host "数据保存在 data\ 目录，未被删除。"
