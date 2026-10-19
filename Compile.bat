@echo off
echo ==========================================
echo  Compiling Zuvio Rollcall Bot to EXE...
echo  (This may take a few minutes)
echo ==========================================
echo.

REM Build inside a clean virtual environment that only has this project's
REM dependencies. Building with the global Python can drag in unrelated
REM packages (e.g. torch / scipy) and bloat the exe to hundreds of MB.
REM The venv and temp build files live outside the (possibly OneDrive-synced)
REM project folder so syncing does not slow the build down.
set "BUILD_VENV=%LOCALAPPDATA%\zuvio-build-venv"
set "BUILD_TMP=%TEMP%\zuvio-build"

if not exist "%BUILD_VENV%\Scripts\python.exe" (
    echo Creating build environment...
    python -m venv "%BUILD_VENV%"
    if %ERRORLEVEL% NEQ 0 goto :fail
)

echo Installing dependencies...
"%BUILD_VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
"%BUILD_VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
if %ERRORLEVEL% NEQ 0 goto :fail

"%BUILD_VENV%\Scripts\python.exe" -m PyInstaller --clean --onefile --noconsole ^
    --workpath "%BUILD_TMP%" --specpath "%BUILD_TMP%" --distpath dist Zuvio.py
if %ERRORLEVEL% NEQ 0 goto :fail

echo.
echo Cleaning temporary files...
rmdir /s /q "%BUILD_TMP%" >nul 2>&1
rmdir /s /q __pycache__ >nul 2>&1

echo.
echo ==========================================
echo  SUCCESS!
echo  Executable created at: dist\Zuvio.exe
echo ==========================================
echo.
goto :end

:fail
echo.
echo [ERROR] Compilation failed!

:end
pause
