@echo off
rem Prompt Lab launcher (ASCII only for .bat compatibility)
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto end
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto end
)
echo.
echo  [!] Python was not found on this computer.
echo      1. Install Python 3.10 or newer: https://www.python.org/downloads/
echo      2. IMPORTANT: on the first installer screen, tick "Add python.exe to PATH".
echo      3. Then double-click this file again.
echo.
pause
:end
echo.
echo  Server stopped. Close this window any time.
pause
