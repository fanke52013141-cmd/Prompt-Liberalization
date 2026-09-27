@echo off
>nul chcp 65001
echo 正在停止提示词优化实验室...
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 8620 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){ $p=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if($p -and $p.ProcessName -match 'python'){ Stop-Process -Id $p.Id -Force; Write-Host '已停止。' } else { Write-Host ('端口 8620 被其他程序占用（PID ' + $c.OwningProcess + '），未终止。') } } else { Write-Host '服务未在运行。' }"
ping -n 2 127.0.0.1 >nul
