@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo =======================================================
echo   🐾 ПАТРУЛЬ 🐾 - ВІДПРАВКА НА GITHUB
echo =======================================================
echo Репозиторій: https://github.com/markfen1010-lgtm/diana-patrol.git
echo.
echo Зараз почнеться завантаження коду.
echo Якщо Git попросить авторизацію - оберіть "Sign in with your browser" (1 клік).
echo.
git push -u origin main
echo.
if %ERRORLEVEL% EQU 0 (
    echo =======================================================
    echo [УСПІХ!] Всі файли успішно завантажено на GitHub!
    echo =======================================================
) else (
    echo [Увага] Якщо виникла помилка авторизації, введіть Personal Access Token або увійдіть через браузер.
)
echo.
pause
