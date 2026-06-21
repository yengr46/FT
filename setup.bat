@echo off
setlocal enabledelayedexpansion
title FTAPPS Setup

echo ============================================================
echo  FTAPPS Installer
echo ============================================================
echo.
echo This installer will:
echo   1. Ask where to install FTAPPS
echo   2. Extract the FTAPPS files to that folder
echo   3. Check Python 3.11 is installed
echo   4. Install required Python packages
echo   5. Check that VLC media player is installed
echo.

REM -- Locate zip file ------------------------------------------
set "ZIP_FILE=%~dp0ftapps.zip"
if not exist "%ZIP_FILE%" (
    echo ERROR: Cannot find ftapps.zip in the same folder as this script.
    echo        Expected: %ZIP_FILE%
    echo.
    echo Make sure setup.bat and ftapps.zip are in the same folder,
    echo then run setup.bat again.
    pause
    exit /b 1
)

REM -- Ask for install folder ------------------------------------
echo -- Step 1: Choose installation folder ----------------------
echo.
set "DEFAULT_DIR=C:\FTAPPS"
set /p INSTALL_DIR=Install folder [%DEFAULT_DIR%]:
if "!INSTALL_DIR!"=="" set "INSTALL_DIR=%DEFAULT_DIR%"

REM Strip trailing backslash if present
if "!INSTALL_DIR:~-1!"=="\" set "INSTALL_DIR=!INSTALL_DIR:~0,-1!"

echo.
echo Installing to: !INSTALL_DIR!
echo.

REM -- Create install folder -------------------------------------
if not exist "!INSTALL_DIR!" (
    echo Creating folder: !INSTALL_DIR!
    mkdir "!INSTALL_DIR!"
    if errorlevel 1 (
        echo ERROR: Could not create folder !INSTALL_DIR!
        echo        Check you have permission to write there.
        pause
        exit /b 1
    )
)

REM -- Extract zip -----------------------------------------------
echo -- Step 2: Extracting files ---------------------------------
echo.
echo Extracting ftapps.zip to !INSTALL_DIR! ...
powershell -NoProfile -Command ^
    "Expand-Archive -LiteralPath '%ZIP_FILE%' -DestinationPath '!INSTALL_DIR!' -Force"
if errorlevel 1 (
    echo.
    echo ERROR: Extraction failed. Make sure ftapps.zip is not corrupted
    echo        and that you have write permission to !INSTALL_DIR!
    pause
    exit /b 1
)
echo Done.
echo.

REM -- Check Python ----------------------------------------------
echo -- Step 3: Checking Python ----------------------------------
echo.
python --version 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo.
    echo Please install Python 3.11 64-bit from:
    echo   https://www.python.org/downloads/release/python-3119/
    echo.
    echo IMPORTANT: Tick "Add Python to PATH" during install,
    echo            then run this setup script again.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%
echo.

REM -- Install Python packages -----------------------------------
echo -- Step 4: Installing Python packages -----------------------
echo.
set "REQ_FILE=!INSTALL_DIR!\requirements.txt"
if not exist "!REQ_FILE!" (
    echo WARNING: requirements.txt not found at !REQ_FILE!
    echo          Skipping package installation.
) else (
    python -m pip install --upgrade pip --quiet
    python -m pip install -r "!REQ_FILE!"
    if errorlevel 1 (
        echo.
        echo ERROR: Package installation failed. Check the output above.
        pause
        exit /b 1
    )
    echo.
    echo Packages installed successfully.
)
echo.

REM -- Check VLC -------------------------------------------------
echo -- Step 5: Checking VLC -------------------------------------
echo.
set VLC_FOUND=0
if exist "C:\Program Files\VideoLAN\VLC\vlc.exe"       set VLC_FOUND=1
if exist "C:\Program Files (x86)\VideoLAN\VLC\vlc.exe" set VLC_FOUND=1

if "!VLC_FOUND!"=="1" (
    echo VLC media player detected.
) else (
    echo WARNING: VLC media player was not found.
    echo.
    echo FTAPPS uses VLC for video playback. Please install the
    echo 64-bit version from:
    echo   https://www.videolan.org/vlc/
    echo.
    echo Make sure to choose the 64-bit installer to match Python.
)
echo.

REM -- Done ------------------------------------------------------
echo ============================================================
echo  Installation complete!
echo.
echo  FTAPPS is installed at:
echo    !INSTALL_DIR!
echo.
echo  To launch FTAPPS, run:
echo    python "!INSTALL_DIR!\main\FTMenu.py"
if "!VLC_FOUND!"=="0" (
    echo.
    echo  Remember to install VLC (64-bit) before using video features.
)
echo ============================================================
echo.
pause
