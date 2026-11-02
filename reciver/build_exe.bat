@echo off
setlocal
REM Builds the scanner app and the standalone receiver app into .exe files.
REM Run this ON WINDOWS, in the same folder as:
REM   lan_core.py, lan_messenger.py  (used together, for the scanner)
REM   lan_receiver.py                (fully standalone, for the receiver)
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
echo Confirming the script files are here...
echo ============================================
if not exist lan_core.py (
    echo ERROR: lan_core.py not found. Needed by lan_messenger.py.
    pause
    exit /b 1
)
if not exist lan_messenger.py (
    echo ERROR: lan_messenger.py not found in this folder.
    pause
    exit /b 1
)
if not exist lan_receiver.py (
    echo ERROR: lan_receiver.py not found in this folder.
    pause
    exit /b 1
)
echo OK - all files found.
echo.

echo ============================================
echo Installing/upgrading PyInstaller...
echo ============================================
python -m pip install --upgrade pyinstaller
if %errorlevel% neq 0 (
    echo.
    echo ERROR: pip install failed - see the message above.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================
echo Building LAN_Messenger.exe (scanner + sender) ...
echo ============================================
python -m PyInstaller --onefile --windowed --name LAN_Messenger lan_messenger.py
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed for lan_messenger.py - scroll up for the actual
    echo PyInstaller error message.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================
echo Building LAN_Receiver.exe (receive + reply only, single file) ...
echo ============================================
python -m PyInstaller --onefile --windowed --name LAN_Receiver lan_receiver.py
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed for lan_receiver.py - scroll up for the actual
    echo PyInstaller error message.
    echo.
    pause
    exit /b 1
)
echo.

echo ============================================
echo SUCCESS. Find the .exe files here:
echo   dist\LAN_Messenger.exe   (for you - scan + send)
echo   dist\LAN_Receiver.exe    (hand out to everyone else - receive + reply)
echo ============================================
pause