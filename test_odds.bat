@echo off
rem ============================================================
rem  CS2 Bot - bookmaker odds test (Stage 2 diagnostics)
rem  Just double-click this file. No manual commands needed.
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

if not exist .env (
    echo.
    echo [ERROR] .env file not found. Copy it from your old bot folder.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   CS2 BOT: BOOKMAKER ODDS TEST (Pari/Fonbet/Winline/Betboom)
echo ============================================================
echo  A visible Firefox window may open - DO NOT touch it,
echo  just wait. Whole test takes about 10-60 seconds.
echo.
venv\Scripts\python.exe test_odds.py

echo.
echo Done. If some bookmaker gave 0 pairs - look into debug_odds
echo folder and send those .json files to the developer.
echo.
pause
