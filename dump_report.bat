@echo off
rem ============================================================
rem  CS2 Bot - compact report from debug_odds dumps (Stage 2)
rem  Double-click. Opens dump_report.txt in Notepad when done.
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

if not exist debug_odds (
    echo.
    echo No debug_odds folder - nothing to report.
    echo It appears only when some bookmaker data was not parsed.
    echo.
    pause
    exit /b 1
)

venv\Scripts\python.exe dump_report.py

echo.
echo Done. dump_report.txt should open in Notepad now.
echo Copy ALL text from it and paste it into the chat.
echo.
pause
