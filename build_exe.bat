@echo off
setlocal
REM Builds the single unified LAN Chat app into one .exe.
REM Run this ON WINDOWS, in the same folder as Netchat.py.
REM Double-click it, or run it from a Command Prompt.

echo ============================================
echo Checking Python is available...
echo ============================================
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ERROR: "python" was not found on your PATH.
    echo Try opening a Command Prompt and running "py --version" instead -
    echo if THAT works, rename every "python" below to "py" and rerun.
    echo.
    pause
    exit /b 1
)
python --version
echo.

echo ============================================
echo Confirming Netchat.py is here...
echo ============================================
if not exist Netchat.py (
    echo ERROR: Netchat.py not found in this folder.
    pause
    exit /b 1
)
echo OK.
echo.

echo ============================================
echo Installing/upgrading PyInstaller and media dependencies...
echo ============================================
python -m pip install --upgrade pyinstaller pillow numpy sounddevice
if %errorlevel% neq 0 (
    echo.
    echo ERROR: pip install failed - see the message above.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================
echo Building LAN_Chat.exe ...
echo ============================================
python -m PyInstaller --onefile --windowed --name LAN_Chat Netchat.py
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed - scroll up for the actual PyInstaller error message.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================
echo SUCCESS. Find it here:
echo   dist\LAN_Chat.exe
echo ============================================
pause