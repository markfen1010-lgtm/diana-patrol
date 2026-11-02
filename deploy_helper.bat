@echo off
chcp 65001 >nul
echo =======================================================
echo   🐾 ПАТРУЛЬ 🐾 - ДЕПЛОЙ НА GITHUB ТА RENDER.COM
echo =======================================================
echo.
set /p REPO_URL="Введіть URL вашого репозиторію на GitHub (наприклад, https://github.com/USER/diana-patrol.git): "
if "%REPO_URL%"=="" (
    echo [Помилка] URL не введено.
    pause
    exit /b
)

echo.
echo [1/3] Налаштування віддаленого репозиторію...
git remote remove origin 2>nul
git remote add origin %REPO_URL%
git branch -M main

echo [2/3] Завантаження файлів на GitHub...
git push -u origin main

if %ERRORLEVEL% EQU 0 (
    echo.
    echo =======================================================
    echo [УСПІХ!] Репозиторій успішно опубліковано на GitHub!
    echo Тепер на Render.com:
    echo 1. Перейдіть на https://dashboard.render.com
    echo 2. Натисніть New + -> Web Service
    echo 3. Підключіть цей репозиторій або вставте посилання
    echo 4. Натисніть "Deploy Web Service"
    echo =======================================================
) else (
    echo.
    echo [Помилка] Не вдалося завантажити на GitHub. Перевірте посилання або авторизацію.
)
echo.
pause
