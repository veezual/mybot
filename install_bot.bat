@echo off
cd /d "%~dp0"
echo ==========================================
echo   CS2 ANALYTICS BOT: SETUP
echo ==========================================
echo.

echo [1/4] Creating venv...
if exist venv (
    echo    venv already exists, skipping
) else (
    py -3.12 -m venv venv
    if errorlevel 1 py -3 -m venv venv
    if errorlevel 1 python -m venv venv
    if errorlevel 1 (
        echo    ERROR: Python not found!
        echo    Install Python 3.12 from python.org ^(check "Add to PATH"^) and retry.
        pause
        exit /b 1
    )
)
call venv\Scripts\activate.bat

echo.
echo [2/4] Installing dependencies (2-3 min)...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed. Check internet connection.
    pause
    exit /b 1
)

echo.
echo [3/4] Downloading Camoufox browser (~1.5 GB, 5-10 min)...
python -m camoufox fetch
if errorlevel 1 (
    echo ERROR: browser download failed.
    echo Check internet and retry manually:  python -m camoufox fetch
    pause
    exit /b 1
)

echo.
echo [4/4] Initializing database...
python database.py

echo.
echo ==========================================
echo   DONE! Now run: run_bot.bat
echo ==========================================
pause
