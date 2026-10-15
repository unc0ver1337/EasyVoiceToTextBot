@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 install.py
) else (
  python install.py
)
if errorlevel 1 (
  echo Setup failed. Read the error above.
  pause
  exit /b 1
)
pause
