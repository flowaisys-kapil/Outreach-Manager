@echo off
cd /d "%~dp0"

echo ===================================================
echo             OUTREACH MANAGER RESET AND SETUP
echo ===================================================
echo.
echo WARNING: This script will perform a CLEAN reset of the application.
echo.
echo CONSEQUENCES OF THIS ACTION:
echo 1. WIPE DATABASE: All existing campaigns, lead lists, CRM deals, and 
echo    chat message history will be permanently deleted from the database.
echo 2. RESET LOGS: Existing background log files will be cleared.
echo 3. KEEP LOGIN: Your cached LinkedIn login session/cookies (chrome_profile) 
echo    will be preserved. You will NOT need to log in to LinkedIn again.
echo 4. RUN ONBOARDING: It will trigger the interactive terminal wizard 
echo    to re-configure your campaign settings, company description, objectives,
echo    and LLM API credentials.
echo.

set /p confirm="Are you sure you want to proceed? (y/N): "
if /i "%confirm%" neq "y" (
    echo.
    echo Reset cancelled.
    pause
    exit /b
)

echo.
echo [1/5] Stopping any running Django or python processes...
:: Gracefully kill any running python processes tied to django
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000') do (
    taskkill /F /PID %%a 2>nul
)
taskkill /F /IM python.exe /FI "WINDOWTITLE eq CRM*" 2>nul

echo.
echo [2/5] Cleaning database and log files...
if exist "data\db.sqlite3" (
    del /f /q "data\db.sqlite3"
    echo      [x] Database deleted (db.sqlite3)
)
if exist "data\outreach.log" (
    del /f /q "data\outreach.log"
    echo      [x] Log file deleted (outreach.log)
)
if exist "data\daemon.pid" (
    del /f /q "data\daemon.pid"
    echo      [x] Process ID file deleted (daemon.pid)
)
if exist "data\django_stdout.log" (
    del /f /q "data\django_stdout.log"
)
if exist "data\django_stderr.log" (
    del /f /q "data\django_stderr.log"
)
echo      [x] Preserved Chrome profile cache (data\chrome_profile)

echo.
echo [3/5] Resolving Python virtual environment...
set "PYTHON_EXE="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
    "%~dp0.venv\Scripts\python.exe" -c "import django" >nul 2>nul
    if errorlevel 1 (
        echo Installing dependencies into .venv...
        "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements\base.txt"
    )
) else (
    echo Virtual environment not found. Creating .venv...
    python -m venv "%~dp0.venv" 2>nul || py -m venv "%~dp0.venv"
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
        echo Installing dependencies into .venv...
        "%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
        "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements\base.txt"
    )
)



if "%PYTHON_EXE%"=="" where python >nul 2>nul && set "PYTHON_EXE=python"
if "%PYTHON_EXE%"=="" where py >nul 2>nul && set "PYTHON_EXE=py"

if not "%PYTHON_EXE%"=="" goto :PYTHON_FOUND

echo ERROR: Python environment could not be found or created.
echo Please install Python 3.11+ and ensure it is in your PATH.
pause
exit /b 1

:PYTHON_FOUND

echo Re-creating database schema (migrations)...
"%PYTHON_EXE%" manage.py migrate --no-input

echo.
echo [4/5] Launching Onboarding Wizard...
echo Please follow the prompts to configure campaigns, products, objectives, and LLM keys.
echo (Use Ctrl+B to go back, Ctrl+D to skip optional, Ctrl+C to cancel)
echo ----------------------------------------------------------------------
"%PYTHON_EXE%" manage.py rundaemon --exit-on-empty



echo.
echo ----------------------------------------------------------------------
echo [5/5] Setup and configuration complete.
echo ===================================================
echo.
set /p run_now="Do you want to run the application now via start.bat? (y/N): "
if /i "%run_now%"=="y" (
    echo Launching start.bat...
    call start.bat
) else (
    echo You can start the app later by running start.bat.
    pause
)

