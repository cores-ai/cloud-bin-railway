@echo off
title MOMO - Railway Telegram Intake Bot
color 0B
cd /d "%~dp0"
cls
echo =====================================================================
echo       MOMO - TELEGRAM INTAKE BOT (RAILWAY READY)
echo =====================================================================
echo.
python bot.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Bot exited with code %ERRORLEVEL%
)
echo.
pause
