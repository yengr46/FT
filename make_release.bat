@echo off
setlocal enabledelayedexpansion
title FTAPPS – Build Release Zip
 
echo ============================================================
echo  FTAPPS – Build Release  (make_release.bat)
echo ============================================================
echo.
echo This script packages the FTAPPS source files into ftapps.zip
echo ready to be distributed alongside setup.bat.
echo.
echo What IS included:
echo   main\        Python files only  (no user data / Database / ini)
echo   helpers\     Python files only  (no ini / log files)
echo   libraries\   Python files only  (no .bak / log files)
echo   requirements.txt
echo   INSTALL.md
echo   run_ftmenu.bat
echo.
echo What is NOT included:
echo   .git  backups  *.zip  *.ini  *.log  __pycache__  *.bak
echo   main\Database  main\ContactSheets  main\FTProj_*  FT_IPC
echo.
 
REM ── Paths ─────────────────────────────────────────────────────
set "SRC=%~dp0"
REM Strip trailing backslash
if "!SRC:~-1!"=="\" set "SRC=!SRC:~0,-1!"
 
set "OUT=!SRC!\ftapps.zip"
set "STAGE=%TEMP%\ftapps_stage_%RANDOM%"
 
echo Source : !SRC!
echo Output : !OUT!
echo Staging: !STAGE!
echo.
 
REM ── Clean up any previous staging folder ──────────────────────
if exist "!STAGE!" rmdir /s /q "!STAGE!"
mkdir "!STAGE!"
 
REM ══════════════════════════════════════════════════════════════
REM  WHAT GOES IN THE ZIP
REM  To add a new app:  add a robocopy line for its folder below.
REM  To add a new top-level file: add a copy line in the
REM  "Top-level files" section.
REM ══════════════════════════════════════════════════════════════
 
REM ── main\ — Python files only ─────────────────────────────────
REM    Excludes: Database, ContactSheets, FTProj_*, FT_IPC, __pycache__
echo Staging main\ ...
robocopy "!SRC!\main" "!STAGE!\main" *.py ^
    /S ^
    /XD __pycache__ Database ContactSheets FT_IPC ^
    /XD FTProj_* ^
    /NFL /NDL /NJH /NJS /NC /NS
REM    /XD "!SRC!\main\FTProj_*" ^
if !errorlevel! GEQ 8 (
    echo ERROR: robocopy failed on main\  ^(exit !errorlevel!^)
    goto :fail
)
 
REM ── helpers\ — Python files only ──────────────────────────────
REM    Excludes: ini files, log files, machine-specific ini, FT_IPC
REM    To add a new helper app: it will be picked up automatically
REM    as long as it is a .py file in this folder.
echo Staging helpers\ ...
robocopy "!SRC!\helpers" "!STAGE!\helpers" *.py ^
    /S ^
    /XD __pycache__ FT_IPC ^
    /NFL /NDL /NJH /NJS /NC /NS
if !errorlevel! GEQ 8 (
    echo ERROR: robocopy failed on helpers\  ^(exit !errorlevel!^)
    goto :fail
)
 
REM ── libraries\ — Python files only ────────────────────────────
REM    Excludes: .bak files, log files, __pycache__
REM    To add a new library: it will be picked up automatically
REM    as long as it is a .py file in this folder.
echo Staging libraries\ ...
robocopy "!SRC!\libraries" "!STAGE!\libraries" *.py ^
    /S ^
    /XD __pycache__ ^
    /NFL /NDL /NJH /NJS /NC /NS
if !errorlevel! GEQ 8 (
    echo ERROR: robocopy failed on libraries\  ^(exit !errorlevel!^)
    goto :fail
)
 
REM ── Top-level files ────────────────────────────────────────────
REM    Add a copy line here for any new top-level file to include.
echo Staging top-level files...
copy "!SRC!\requirements.txt" "!STAGE!\" >nul
copy "!SRC!\INSTALL.md"       "!STAGE!\" >nul
copy "!SRC!\run_ftmenu.bat"   "!STAGE!\" >nul
 
REM ══════════════════════════════════════════════════════════════
 
REM ── Report what will be zipped ────────────────────────────────
echo.
echo Files staged:
dir /s /b "!STAGE!" | find /c /v ""
echo file(s)
echo.
 
REM ── Remove old zip ────────────────────────────────────────────
if exist "!OUT!" (
    echo Removing old ftapps.zip ...
    del "!OUT!"
)
 
REM ── Create zip ────────────────────────────────────────────────
echo Creating ftapps.zip ...
powershell -NoProfile -Command ^
    "Compress-Archive -Path '!STAGE!\*' -DestinationPath '!OUT!' -Force"
if errorlevel 1 (
    echo ERROR: Compress-Archive failed.
    goto :fail
)
 
REM ── Clean up staging ──────────────────────────────────────────
rmdir /s /q "!STAGE!"
 
REM ── Done ──────────────────────────────────────────────────────
echo.
for %%F in ("!OUT!") do set ZIP_SIZE=%%~zF
set /a ZIP_KB=!ZIP_SIZE! / 1024
echo ============================================================
echo  ftapps.zip created successfully  (!ZIP_KB! KB)
echo  Location: !OUT!
echo.
echo  Distribute setup.bat + ftapps.zip together.
echo ============================================================
echo.
pause
exit /b 0
 
:fail
if exist "!STAGE!" rmdir /s /q "!STAGE!"
echo.
echo Build failed. See error above.
pause
exit /b 1