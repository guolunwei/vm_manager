@echo off
REM 一条命令同时启动 Web 与后台 worker（优先使用项目内 .venv）
REM
REM   start.bat                 开发模式（serve：web + worker 同窗口，Ctrl+C 一起退）
REM   start.bat 0.0.0.0:8000    指定监听地址
REM   start.bat prod            生产模式（waitress + prod 设置，worker 另开窗口）
REM
REM 没有 worker 消费队列的话，页面上点「创建」会一直停在「排队中」。
setlocal
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

set MODE=dev
set HOSTPORT=127.0.0.1:8000
if /i "%1"=="prod" (
  set MODE=prod
) else if not "%1"=="" (
  set HOSTPORT=%1
)

if /i "%MODE%"=="prod" (
  echo [1/2] 启动后台 worker（新窗口）...
  start "VM Manager Worker" cmd /k ""%PY%" manage.py worker"
  echo [2/2] 收集静态文件...
  set DJANGO_SETTINGS_MODULE=config.settings.prod
  "%PY%" manage.py collectstatic --noinput >nul
  echo [2/2] 启动 Web（waitress）http://%HOSTPORT%
  "%PY%" -m waitress --listen=%HOSTPORT% config.wsgi:application
) else (
  echo 启动 Web + worker（同一窗口，Ctrl+C 一起退出）http://%HOSTPORT%
  "%PY%" manage.py serve %HOSTPORT%
)

endlocal
