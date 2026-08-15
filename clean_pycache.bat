@echo off
setlocal enabledelayedexpansion

:: Navigate to project root directory
cd /d "%~dp0"

echo ===================================================
echo             REMOVING PYCACHE FILES
echo ===================================================
echo.

set "COUNT=0"

:: Delete all __pycache__ directories (ignoring .venv)
for /d /r "%~dp0" %%d in (__pycache__) do (
    if exist "%%d" (
        set "DIRPATH=%%d"
        echo !DIRPATH! | findstr /i "\\.venv\\" >nul
        if errorlevel 1 (
            echo Removing directory: %%d
            rd /s /q "%%d" 2>nul
            set /a COUNT+=1
        )
    )
)

:: Delete compiled .pyc and .pyo files outside .venv
for /r "%~dp0" %%f in (*.pyc *.pyo) do (
    if exist "%%f" (
        set "FILEPATH=%%f"
        echo !FILEPATH! | findstr /i "\\.venv\\" >nul
        if errorlevel 1 (
            del /f /q "%%f" 2>nul
        )
    )
)

echo.
echo Clean complete. Removed cache directories.
pause
