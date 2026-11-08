@echo off
REM ===========================================================================
REM  daily.bat - the daily content cycle
REM  Register with Windows Task Scheduler to run once a day.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."

REM --- load .env into the environment ---------------------------------------
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v /b "#" ".env"`) do (
        if not "%%A"=="" set "%%A=%%B"
    )
)

if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: run scripts\setup.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

if not exist "output\logs" mkdir "output\logs"
set STAMP=%DATE:~0,4%-%DATE:~5,2%-%DATE:~8,2%
set LOG=output\logs\%STAMP%.log

echo ===== %DATE% %TIME% ===== >> "%LOG%"

REM --- 1. poll feeds (optional; skip if you publish AI-only content) --------
echo [1/4] fetching reference material...
python run.py fetch >> "%LOG%" 2>&1

REM --- 2. write N articles per site -----------------------------------------
REM     --count is PER SITE. 8 sites x 3 = 24 articles a day.
echo [2/4] generating articles...
python run.py gen --count 3 >> "%LOG%" 2>&1

REM --- 3. push to WordPress as drafts ---------------------------------------
REM     Keep status=draft for the first 8 weeks so you can eyeball every post.
REM     Once quality is stable, switch to --wp-status publish.
echo [3/4] publishing to WordPress...
python run.py publish --count 40 --wp-status draft >> "%LOG%" 2>&1

REM --- 4. sitemap + IndexNow ------------------------------------------------
echo [4/4] submitting for indexing...
python run.py submit >> "%LOG%" 2>&1

echo [done]
python run.py status >> "%LOG%" 2>&1
type "%LOG%" | findstr /v "^$" | more +0 > nul

echo Cycle complete. Log: %LOG%
endlocal
