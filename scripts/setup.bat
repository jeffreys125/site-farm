@echo off
REM ===========================================================================
REM  setup.bat - one-time environment setup on Windows
REM  Run this once from the project root:  scripts\setup.bat
REM ===========================================================================
setlocal
cd /d "%~dp0.."

echo [1/3] Creating virtual environment...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Python not found. Install Python 3.10+ and tick "Add to PATH".
        pause
        exit /b 1
    )
)

echo [2/3] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed.
    pause
    exit /b 1
)

echo [3/3] Creating config from template...
if not exist "config\sites.yaml" (
    copy "config\sites.example.yaml" "config\sites.yaml" >nul
    echo   created config\sites.yaml - open it and replace example.com
) else (
    echo   config\sites.yaml already exists, left untouched
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo   created .env - paste your DeepSeek key inside
)

echo.
echo Done. Next steps:
echo   1. Edit config\sites.yaml  (domains, WordPress users)
echo   2. Edit .env               (DEEPSEEK_API_KEY, WP passwords)
echo   3. Run: scripts\daily.bat
pause
