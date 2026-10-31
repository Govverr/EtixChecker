@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ETIX CHECKER - ТЕСТОВОЕ ОБНОВЛЕНИЕ ANTI-BAN

echo ======================================================
echo  Подключение тестовой версии Anti-Ban
echo  (ветка: feat/datadome-advanced-anti-ban)
echo ======================================================
echo.

where git >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ОШИБКА] Утилита Git не найдена в системе.
    echo Убедитесь, что Git установлен и добавлен в PATH.
    echo.
    pause
    exit /b 1
)

if not exist ".git" (
    echo [ОШИБКА] Папка .git не найдена в текущей директории.
    echo Скрипт должен находиться в корневой папке Etix Checker.
    echo.
    pause
    exit /b 1
)

echo [1/3] Запрос обновлений ветки с GitHub...
git fetch origin feat/datadome-advanced-anti-ban
if %ERRORLEVEL% NEQ 0 (
    echo [ОШИБКА] Не удалось связаться с GitHub или ветка не найдена.
    echo.
    pause
    exit /b 1
)

echo [2/3] Переключение на ветку feat/datadome-advanced-anti-ban...
git checkout feat/datadome-advanced-anti-ban
if %ERRORLEVEL% NEQ 0 (
    echo [ОШИБКА] Сбой при переключении на ветку.
    echo.
    pause
    exit /b 1
)

echo [3/3] Загрузка актуального кода...
git pull --no-rebase origin feat/datadome-advanced-anti-ban

echo.
echo ======================================================
echo  [УСПЕХ] Тестовая версия с защитой DataDome установлена!
echo  В интерфейсе теперь доступна кнопка "Собрать логи".
echo  Можете запускать run_gui.bat как обычно.
echo ======================================================
echo.
pause
