@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ETIX CHECKER - ВОЗВРАТ НА СТАБИЛЬНУЮ ВЕРСИЮ (MAIN)

echo ======================================================
echo  Возврат на стабильную версию (ветка main)
echo ======================================================
echo.

where git >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ОШИБКА] Git не найден в системе.
    pause
    exit /b 1
)

echo [1/2] Запрос ветки main с GitHub...
git fetch origin main

echo [2/2] Переключение на ветку main...
git checkout main
git pull --no-rebase origin main

echo.
echo ======================================================
echo  [УСПЕХ] Вы вернулись на стабильную ветку main!
echo ======================================================
echo.
pause
