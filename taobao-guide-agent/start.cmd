@echo off
setlocal
cd /d "%~dp0" || goto failed

set "GUIDE_WORK=E:\Codex\work\taobao-guide-agent"
set "TEMP=%GUIDE_WORK%\tmp"
set "TMP=%TEMP%"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "GUIDE_PYTHON=%GUIDE_WORK%\venv\Scripts\python.exe"

if not exist "%TEMP%\" mkdir "%TEMP%"
if not exist "%TEMP%\" goto failed
if not exist "%GUIDE_PYTHON%" (
    python -m venv "%GUIDE_WORK%\venv"
    if errorlevel 1 goto failed
)

"%GUIDE_PYTHON%" -B -c "import fastapi, uvicorn, httpx" >nul 2>&1
if errorlevel 1 (
    "%GUIDE_PYTHON%" -B -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
    if errorlevel 1 goto failed
)

echo Xunwu Shopping Guide - http://127.0.0.1:18766/
"%GUIDE_PYTHON%" -B app.py
if errorlevel 1 goto failed
exit /b 0

:failed
echo Startup failed. See the message above and README.md.
pause
exit /b 1
