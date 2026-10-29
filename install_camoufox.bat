@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
echo ==========================================
echo   CAMOUFOX BROWSER INSTALLER
echo ==========================================
echo.

echo [1/2] Checking if browser is already installed...
python -c "from camoufox.pkgman import installed_verstr; print('  INSTALLED: v' + installed_verstr())" 2>nul
if not errorlevel 1 (
    echo.
    echo Browser is already installed. Nothing to do.
    echo Run: run_bot.bat
    pause
    exit /b 0
)
echo   Not installed yet.
echo.

echo [2/2] Downloading browser (~1.5 GB, 5-10 min)...
echo If you see red 403 errors - GitHub API rate limit is hit. Wait 30-60 min and retry.
echo Tip: set a free GitHub token for high limits, see instructions at the end.
echo.
python -m camoufox fetch

python -c "from camoufox.pkgman import installed_verstr; print('  INSTALLED: v' + installed_verstr())" 2>nul
if errorlevel 1 (
    echo.
    echo ==========================================
    echo   BROWSER NOT INSTALLED
    echo ==========================================
    echo The download did not finish. Most likely GitHub API rate limit (403).
    echo.
    echo Options:
    echo   1. Wait 30-60 minutes, then run this file again.
    echo   2. Use a free GitHub token (5000x higher limit):
    echo      a) Create token at: https://github.com/settings/tokens
    echo         ^(no scopes needed, "Generate new token"^)
    echo      b) In this window run:
    echo           set GITHUB_TOKEN=your_token_here
    echo      c) Then run this file again in the SAME window.
    echo   3. Or change IP: restart router / use mobile hotspot / VPN.
    echo.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   DONE! Browser installed. Run: run_bot.bat
echo ==========================================
pause
