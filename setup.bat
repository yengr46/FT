@echo off
setlocal enabledelayedexpansion
title FTAPPS Setup

echo ============================================================
echo  FTAPPS Setup
echo ============================================================
echo.

REM ── Check Python 3.11 64-bit ─────────────────────────────────
echo Checking Python...
python --version 2>nul
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.11 64-bit from:
    echo   https://www.python.org/downloads/release/python-3119/
    echo Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

REM ── Install pip packages ──────────────────────────────────────
echo.
echo Installing Python packages...
python -m pip install --upgrade pip --quiet
python -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: Package installation failed. Check the output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Python packages installed successfully.
echo ============================================================
echo.
echo NEXT STEP — Install VLC media player (64-bit):
echo   https://www.videolan.org/vlc/
echo   Choose the 64-bit installer. Must match your Python (64-bit).
echo.
echo After VLC is installed, launch FTAPPS with:
echo   python "%~dp0main\FTMenu.py"
echo.
echo Or double-click run_ftmenu.bat in this folder.
echo ============================================================
echo.
pause
