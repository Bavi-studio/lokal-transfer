@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python Launcher 'py' not found. Install Python 3.13 or 3.14 from python.org and enable the launcher.
  pause
  exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating project virtual environment...
  py -3.13 -m venv .venv 2>nul
  if errorlevel 1 py -3.14 -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create a virtual environment. Install Python 3.13 or 3.14 and retry.
    pause
    exit /b 2
  )
)

.venv\Scripts\python.exe -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo Installing required packages from requirements.txt...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check internet/proxy access or install packages from a local wheelhouse.
    pause
    exit /b 2
  )
)

.venv\Scripts\python.exe scripts\startup.py
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
  echo.
  echo Server launcher exited with code %EXITCODE%.
  pause
)
exit /b %EXITCODE%
