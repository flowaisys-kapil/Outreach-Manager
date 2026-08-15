@echo off
setlocal enabledelayedexpansion

:: Navigate to script directory
cd /d "%~dp0"

echo ===================================================
echo             STARTING OUTREACH MANAGER
echo ===================================================
echo.

:: Detect Python environment
set "PYTHON_EXE="

:: 1. Check for project virtual environment (.venv)
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import django" >nul 2>nul && set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

:: 2. Check system python if .venv missing or invalid
if "%PYTHON_EXE%"=="" where python >nul 2>nul && python -c "import django" >nul 2>nul && set "PYTHON_EXE=python"
if "%PYTHON_EXE%"=="" where py >nul 2>nul && py -c "import django" >nul 2>nul && set "PYTHON_EXE=py"

:: 3. If Python with Django is found, proceed directly to start
if not "%PYTHON_EXE%"=="" goto :PYTHON_FOUND

echo.
echo WARNING: Required Python environment or dependencies (Django) are missing.
echo Automatically running setup.bat to initialize environment...
echo ----------------------------------------------------------------------
call "%~dp0setup.bat"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -c "import django" >nul 2>nul && set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

if not "%PYTHON_EXE%"=="" goto :PYTHON_FOUND

echo ERROR: Setup failed or Python environment is missing.
pause
exit /b 1

:PYTHON_FOUND

:: Free port 8000 if occupied
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo Terminating process %%a occupying port 8000...
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting Django CRM Web Application...
echo.

:: Open browser after 3 seconds
start /b cmd /c "timeout /t 3 >nul & start http://localhost:8000/"

:: Launch Django server
"%PYTHON_EXE%" manage.py runserver 8000


pause
