@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM First-time setup for SE Radio Client
REM - Ensures Python 3 is installed (downloads 3.13.0 if needed)
REM - Creates/updates a local venv
REM - Installs Python dependencies (requirements.txt or defaults)
REM - Launches the normal client launcher when finished
REM ============================================================

cd /d "%~dp0"

set "PY_VERSION=3.13.0"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
set "PY_SHORT=313"

echo [SE-Radio] Checking for Python 3...
call :find_python

if not defined PY_CMD (
    echo [SE-Radio] Python not found. Downloading %PY_VERSION% (x64)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%'" || goto :error

    echo [SE-Radio] Installing Python %PY_VERSION% (per-user)...
    start /wait "" "%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1 InstallLauncherAllUsers=0 || goto :error
    del "%PY_INSTALLER%" >nul 2>nul

    REM Refresh Python detection after installation
    call :find_python
)

if not defined PY_CMD (
    echo [SE-Radio] ERROR: Python was not found even after the installer ran.
    goto :error
)

echo [SE-Radio] Using Python launcher: %PY_CMD%
"%PY_CMD%" --version >nul 2>nul || goto :error

REM Create virtual environment if missing
if not exist "venv\Scripts\python.exe" (
    echo [SE-Radio] Creating virtual environment...
    "%PY_CMD%" -m venv venv || goto :error
)

REM Activate venv
call "venv\Scripts\activate.bat" || goto :error

echo [SE-Radio] Upgrading pip...
python -m pip install --upgrade pip || goto :error
python -m pip install --upgrade setuptools wheel || goto :error

REM Install dependencies
if exist "requirements.txt" (
    echo [SE-Radio] Installing/updating Python packages from requirements.txt...
    python -m pip install --upgrade -r requirements.txt || goto :error
    REM Ensure global hotkeys fallback lib is present
    python -m pip install --upgrade keyboard pynput || goto :error
) else (
    echo [SE-Radio] requirements.txt not found; installing expected defaults...
    python -m pip install --upgrade pygame numpy sounddevice websockets pynput keyboard requests || goto :error
)

echo [SE-Radio] Setup complete. Launching the client...
start "" "%~dp0run_radio_client.bat"
goto :eof

:find_python
set "PY_CMD="
for %%P in (py python) do (
    if not defined PY_CMD (
        %%P --version >nul 2>nul && set "PY_CMD=%%P"
    )
)

if not defined PY_CMD (
    for %%P in ("%LocalAppData%\Programs\Python\Python%PY_SHORT%\python.exe" "%ProgramFiles%\Python%PY_SHORT%\python.exe" "%ProgramFiles(x86)%\Python%PY_SHORT%\python.exe" "C:\Python%PY_SHORT%\python.exe") do (
        if not defined PY_CMD (
            if exist "%%~P" (
                set "PY_CMD=%%~fP"
            )
        )
    )
)
exit /b 0

:error
echo.
echo [SE-Radio] Setup failed. Review the messages above for details.
echo Press any key to exit.
pause >nul
exit /b 1
