
@echo off
setlocal EnableExtensions

title LAN Chat - EXE Builder

echo ============================================
echo          LAN CHAT EXE BUILDER
echo ============================================
echo.

REM Check Python
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    pause
    exit /b 1
)

python --version
echo.

REM Check source file
if not exist "lan_chat.py" (
    echo ERROR: lan_chat.py not found!
    echo Place this BAT file in the same folder.
    pause
    exit /b 1
)

echo ============================================
echo Installing required libraries...
echo ============================================

python -m pip install --upgrade pip
if errorlevel 1 goto ERROR

python -m pip install --upgrade pyinstaller pillow numpy sounddevice cffi
if errorlevel 1 goto ERROR

echo.
echo ============================================
echo Verifying media libraries...
echo ============================================

python -c "import PIL; import numpy; import sounddevice; import _sounddevice; print('Pillow OK'); print('NumPy OK'); print('SoundDevice OK')"
if errorlevel 1 goto ERROR

echo.
echo ============================================
echo Cleaning previous builds...
echo ============================================

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist LAN_Chat.spec del /q LAN_Chat.spec

echo.
echo ============================================
echo Building standalone LAN_Chat.exe...
echo ============================================

python -m PyInstaller ^
    --clean ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name LAN_Chat ^
    --collect-all PIL ^
    --collect-all numpy ^
    --collect-all sounddevice ^
    --collect-all cffi ^
    --collect-binaries sounddevice ^
    --hidden-import=_sounddevice ^
    --hidden-import=_cffi_backend ^
    lan_chat.py

if errorlevel 1 goto ERROR

echo.
echo ============================================
echo BUILD SUCCESSFUL!
echo ============================================
echo.
echo Your standalone executable:
echo dist\LAN_Chat.exe
echo.
echo Pillow, NumPy and SoundDevice are bundled.
echo End users do not need to install Python libraries.
echo.
pause
exit /b 0

:ERROR
echo.
echo ============================================
echo BUILD FAILED!
echo ============================================
echo Check the error messages above.
pause
exit /b 1
