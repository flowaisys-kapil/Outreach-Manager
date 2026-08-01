@echo off
:: Navigate to the script's directory
cd /d "%~dp0"

echo ===================================================
echo             STARTING OUTREACH MANAGER
echo ===================================================
echo.
:: Detect if port 8000 is occupied and kill the process using it
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
    echo Port 8000 is occupied by process %%a. Terminating zombie process...
    taskkill /f /pid %%a
)

echo Starting Django CRM Web Application...
echo.

:: Launch the default web browser to the dashboard after a short delay
start /b cmd /c "timeout /t 3 >nul && start http://localhost:8000/"

:: Start Django development server using the virtual environment
.venv\Scripts\python.exe manage.py runserver

pause
