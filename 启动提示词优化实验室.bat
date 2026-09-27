@echo off
>nul chcp 65001
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%venv\Scripts\python.exe"
set "URL=http://127.0.0.1:8620"

rem 已在运行则直接打开浏览器
powershell -NoProfile -Command "try{ $null=Invoke-WebRequest -Uri '%URL%/workflow-api/v1/templates' -UseBasicParsing -TimeoutSec 2; exit 0 }catch{ exit 1 }" >nul 2>&1
if not errorlevel 1 goto open

echo 正在启动提示词优化实验室，请稍候...
if not exist "%ROOT%data" mkdir "%ROOT%data"
start "PromptLab Server" /min "%PY%" "%ROOT%run_server.py" --host 127.0.0.1 --port 8620

set /a TRIES=0
:wait
ping -n 2 127.0.0.1 >nul
powershell -NoProfile -Command "try{ $null=Invoke-WebRequest -Uri '%URL%/workflow-api/v1/templates' -UseBasicParsing -TimeoutSec 2; exit 0 }catch{ exit 1 }" >nul 2>&1
if not errorlevel 1 goto open
set /a TRIES+=1
if %TRIES% lss 15 goto wait

echo 启动失败，请查看 data 目录下的日志或重新安装依赖。
pause
exit /b 1

:open
start "" "%URL%"
exit /b 0
