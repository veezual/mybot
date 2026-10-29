@echo off
rem ============================================================
rem  CS2 Bot - check external data sources (PandaScore, API-Sport)
rem  Double-click to run. Copy console output into the chat.
rem ============================================================
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
    echo.
    echo [ERROR] venv not found in this folder.
    echo Run install_bot.bat first, then try again.
    echo.
    pause
    exit /b 1
)

venv\Scripts\python.exe test_datasources.py

echo.
echo Done. Copy ALL text above and paste it into the chat.
pause
