@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. See README.md.
  pause
  exit /b 1
)
if exist stop.flag del stop.flag
:run
".venv\Scripts\python.exe" -u bot.py
if not errorlevel 1 goto end
if exist stop.flag goto end
echo Bot exited with an error. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto run
:end
