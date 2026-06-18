@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Alice Code
set "LOG=setup.log"

REM --- find Python ---
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
  echo         and tick "Add python.exe to PATH" during install, then re-run.
  goto :end
)
%PY% --version

REM --- virtual environment ---
if not exist ".venv\Scripts\python.exe" (
  echo [setup] Creating virtual environment...
  %PY% -m venv .venv >>"%LOG%" 2>&1
  if errorlevel 1 ( echo [ERROR] venv creation failed - see setup.log & goto :end )
)
set "VPY=.venv\Scripts\python.exe"

REM --- dependencies: (re)install if any are missing (self-heals on updates) ---
"%VPY%" -c "import openai, rich, fastapi, uvicorn, httpx, websockets, playwright, prompt_toolkit" >nul 2>&1
if errorlevel 1 (
  echo [setup] Installing/updating dependencies... this can take a few minutes.
  "%VPY%" -m pip install --upgrade pip >>"%LOG%" 2>&1
  "%VPY%" -m pip install -r requirements.txt >>"%LOG%" 2>&1
  if errorlevel 1 ( echo [ERROR] pip install failed - see setup.log & goto :end )
)

REM --- Playwright browser for login (once) ---
if not exist ".venv\.pw_ok" (
  echo [setup] Downloading browser for Yandex login ^(~150 MB, one-time^)...
  "%VPY%" -m playwright install chromium >>"%LOG%" 2>&1
  if errorlevel 1 ( echo [ERROR] browser download failed - see setup.log & goto :end )
  echo ok>".venv\.pw_ok"
)

REM --- .env (defaults are fine; login is automatic) ---
if not exist ".env" copy ".env.example" ".env" >nul

REM --- launch agent ---
echo [run] Starting Alice Code...
"%VPY%" agent.py
if errorlevel 1 echo [ERROR] agent.py exited with an error (see messages above).

:end
echo.
echo ===== This window stays open. Press any key to close it. =====
pause >nul
