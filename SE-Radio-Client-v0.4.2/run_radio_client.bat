@echo off
setlocal ENABLEDELAYEDEXPANSION

REM ====================================================================
REM  SE-Radio Client Launcher
REM  - Ensures Python venv exists
REM  - Upgrades pip
REM  - Installs/updates dependencies from requirements.txt (if present)
REM  - Launches main.py with pythonw (no console window)
REM ====================================================================

REM Change to script directory
cd /d "%~dp0"

REM Choose Python launcher (fallback to "python" if "py" is unavailable)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set PYLAUNCHER=py
) else (
    set PYLAUNCHER=python
)

REM Create venv if it doesn't exist
if not exist "venv\Scripts\python.exe" (
    echo [SE-Radio] Creating virtual environment...
    %PYLAUNCHER% -3 -m venv venv
)

REM Activate venv
call "venv\Scripts\activate.bat"

REM Upgrade pip quietly
echo [SE-Radio] Upgrading pip...
python -m pip install --upgrade pip >nul 2>nul

REM Install / update dependencies if requirements.txt exists
if exist "requirements.txt" (
    echo [SE-Radio] Installing/updating dependencies from requirements.txt...
    python -m pip install --upgrade -r requirements.txt
) else (
    echo [SE-Radio] No requirements.txt found, skipping dependency install.
)

REM Prefer pythonw (no console) if present in the venv
set RUNNER=pythonw.exe
if not exist "venv\Scripts\%RUNNER%" (
    set RUNNER=python.exe
)

echo [SE-Radio] Launching client...
start "" "venv\Scripts\%RUNNER%" "%~dp0main.py"

endlocal
exit /b 0
