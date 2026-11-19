@echo off
setlocal
cd /d "%~dp0"
REM Builds LAN_Chat.exe. If Python or a required package is missing and this
REM PC is online, it downloads and installs it automatically.
REM Put this file in the same folder as Netchat.py (or lan_chat.py).

echo ============================================
echo Looking for the source file...
echo ============================================
set "SRC="
if exist Netchat.py set "SRC=Netchat.py"
if not defined SRC if exist lan_chat.py set "SRC=lan_chat.py"
if not defined SRC (
    echo ERROR: Netchat.py or lan_chat.py not found in this folder.
    pause
    exit /b 1
)
echo Using %SRC%
echo.

echo ============================================
echo Checking internet connection...
echo ============================================
set "ONLINE=0"
powershell -NoProfile -Command "try { Invoke-WebRequest https://pypi.org -UseBasicParsing -TimeoutSec 6 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if %errorlevel% equ 0 set "ONLINE=1"
if "%ONLINE%"=="1" (echo Online.) else (echo Offline - nothing can be downloaded.)
echo.

echo ============================================
echo Checking Python...
echo ============================================
set "PY="
call :find_python
if defined PY goto have_python

echo Python was not found.
if "%ONLINE%"=="0" (
    echo ERROR: Python is missing and there is no internet to download it.
    echo Install Python from python.org, or connect to the internet and run this again.
    pause
    exit /b 1
)
echo Installing Python automatically...
where winget >nul 2>nul
if %errorlevel% equ 0 (
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
)
call :find_python
if defined PY goto have_python

echo winget did not work, downloading the installer from python.org...
powershell -NoProfile -Command "Invoke-WebRequest https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe -OutFile '%TEMP%\python_installer.exe'"
if not exist "%TEMP%\python_installer.exe" (
    echo ERROR: could not download the Python installer.
    pause
    exit /b 1
)
"%TEMP%\python_installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1
call :find_python
if defined PY goto have_python

echo ERROR: Python installation finished but Python still cannot be found.
echo Close this window and run the script again.
pause
exit /b 1

:have_python
"%PY%" --version
echo.

echo ============================================
echo Checking required packages...
echo (pillow, numpy, sounddevice, opencv-python for video calling)
echo ============================================
"%PY%" -m pip --version >nul 2>nul
if %errorlevel% neq 0 "%PY%" -m ensurepip --upgrade >nul 2>nul

"%PY%" -c "import PyInstaller, PIL, numpy, sounddevice, cv2" >nul 2>nul
if %errorlevel% equ 0 (
    echo All packages already installed.
    goto build
)
if "%ONLINE%"=="0" (
    echo ERROR: some packages are missing and there is no internet to download them.
    echo Needed: pyinstaller pillow numpy sounddevice opencv-python
    pause
    exit /b 1
)
echo Installing pyinstaller, pillow, numpy, sounddevice, opencv-python...
echo (opencv-python is a big download - this step can take a while, that's normal)
"%PY%" -m pip install --upgrade pyinstaller pillow numpy sounddevice opencv-python
if %errorlevel% neq 0 (
    echo ERROR: pip install failed - see the message above.
    pause
    exit /b 1
)

:build
echo.
echo ============================================
echo Building LAN_Chat.exe ...
echo (also slower than before because of opencv-python - that's expected)
echo ============================================
"%PY%" -m PyInstaller --onefile --windowed --name LAN_Chat --collect-all cv2 --collect-all sounddevice "%SRC%"
if %errorlevel% neq 0 (
    echo ERROR: Build failed - scroll up for the PyInstaller error.
    pause
    exit /b 1
)
echo.

echo ============================================
echo Verifying the build actually has cv2/Pillow/audio bundled...
echo ============================================
del /q "%~dp0selftest.txt" >nul 2>nul
dist\LAN_Chat.exe --selftest
if exist "%~dp0selftest.txt" (
    type "%~dp0selftest.txt"
) else (
    echo (selftest.txt was not created - the .exe may not support --selftest yet)
)
echo.

echo ============================================
echo SUCCESS. Find it here:
echo   dist\LAN_Chat.exe
echo.
echo Copy it to the other PC. On first run click Allow and tick
echo Private networks in the Windows Firewall popup.
echo ============================================
pause
exit /b 0


REM ---- helper: sets PY if a working Python is found ----
:find_python
set "PY="
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -c "import sys" >nul 2>nul
    if %errorlevel% equ 0 set "PY=py"
)
if defined PY exit /b 0
where python >nul 2>nul
if %errorlevel% equ 0 (
    python -c "import sys" >nul 2>nul
    if %errorlevel% equ 0 set "PY=python"
)
if defined PY exit /b 0
for %%V in (313 312 311 310) do (
    if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PY=%LocalAppData%\Programs\Python\Python%%V\python.exe"
    if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
)
exit /b 0