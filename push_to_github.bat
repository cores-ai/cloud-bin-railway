@echo off
title MOMO - Push Railway Bot to GitHub
color 0A
cd /d "%~dp0"
cls
echo =====================================================================
echo         MOMO - PUSHING RAILWAY BOT TO GITHUB REPOSITORY
echo =====================================================================
echo Repo: https://github.com/cores-ai/cloud-bin-railway
echo Branch: main
echo.
git push -u origin main
echo.
if %ERRORLEVEL% EQU 0 (
    echo [✓] SUCCESS: Code successfully pushed to GitHub!
) else (
    echo [!] Notice: If GitHub asked for login, complete the browser login.
)
echo.
pause
