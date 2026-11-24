@echo off
chcp 65001 >nul
REM One command to start both Web and the background worker (prefers project .venv).
REM
REM   start.bat                 dev mode (serve: web + worker in one window, Ctrl+C exits both)
REM   start.bat 0.0.0.0:8000    bind to a specific address
REM   start.bat prod            prod mode (waitress + prod settings, worker in another window)
REM
REM Without a worker consuming the queue, clicking "create" in the UI stays queued forever.
REM NOTE: this file MUST keep CRLF line endings; cmd.exe mis-parses LF-only multi-line
REM       if-blocks and the window closes instantly. .gitattributes enforces eol=crlf.
setlocal
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found: neither .venv\Scripts\python.exe nor python on PATH.
  echo         Rebuild the virtualenv in the project root:
  echo           python -m venv .venv
  echo           .venv\Scripts\python.exe -m pip install -r requirements.txt
  goto :fail
)

set MODE=dev
set HOSTPORT=127.0.0.1:8000
if /i "%1"=="prod" (
  set MODE=prod
) else if not "%1"=="" (
  set HOSTPORT=%1
)

if /i "%MODE%"=="prod" (
  echo [1/2] Starting background worker in a new window...
  start "VM Manager Worker" cmd /k "%PY% manage.py worker"
  echo [2/2] Collecting static files...
  set DJANGO_SETTINGS_MODULE=config.settings.prod
  "%PY%" manage.py collectstatic --noinput >nul
  echo [2/2] Starting Web (waitress) http://%HOSTPORT%
  "%PY%" -m waitress --listen=%HOSTPORT% config.wsgi:application
) else (
  echo Starting Web + worker in this window, Ctrl+C stops both. http://%HOSTPORT%
  "%PY%" manage.py serve %HOSTPORT%
)

if errorlevel 1 goto :fail

echo.
echo Service stopped. Closing in 3 seconds...
timeout /t 3 >nul
goto :eof

:fail
echo.
echo [ERROR] Startup failed. The messages above are the reason; window stays open.
pause
endlocal
exit /b 1
