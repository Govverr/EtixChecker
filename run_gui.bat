@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title ETIX CHECKER 2026
color 07
mode con: cols=95 lines=30

set "VENV=venv"
set "VPY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps_installed_gui"
set "LOGDIR=logs"
set "LOGFILE=%LOGDIR%\setup.log"

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

rem 1) venv
if not exist "%VPY%" (
  echo [*] Инициализация виртуального окружения (venv)...
  py -3 -m venv "%VENV%" >nul 2>&1 || python -m venv "%VENV%" >nul 2>&1 || (echo [ERROR] Ошибка создания venv & pause & exit /b 1)
)

rem 2) deps
if not exist "%STAMP%" (
  echo [*] Установка зависимостей и Playwright Chromium (пожалуйста, подождите)...
  "%VPY%" -m pip install --upgrade pip -q >>"%LOGFILE%" 2>&1
  "%VPY%" -m pip install -r requirements.txt -q >>"%LOGFILE%" 2>&1

  set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"
  "%VPY%" -m playwright install chromium >>"%LOGFILE%" 2>&1

  >"%STAMP%" echo ok
)

rem 3) playwright cache
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright"

rem 4) run GUI
set "PYTHONUTF8=1"
"%VPY%" gui_app.py
set EC=%ERRORLEVEL%

if /I "%~1"=="--no-pause" (
  exit /b %EC%
)

if %EC% neq 0 (
  echo.
  echo [!] Программа завершилась с кодом ошибки: %EC%
  pause
)
exit /b %EC%
