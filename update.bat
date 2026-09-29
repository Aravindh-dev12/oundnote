@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "OUNDNOTE_PYTHON=%CD%\.venv\Scripts\python.exe"
set "OUNDNOTE_PREPARED=0"
set "OUNDNOTE_RESTART=0"

:parse_options
if /I "%~1"=="--prepared" (
    set "OUNDNOTE_PREPARED=1"
    shift
    goto :parse_options
)
if /I "%~1"=="--restart" (
    set "OUNDNOTE_RESTART=1"
    shift
    goto :parse_options
)
if not "%~1"=="" (
    echo ERROR: Unknown option: %~1
    exit /b 2
)

if not exist "%OUNDNOTE_PYTHON%" (
    echo ERROR: Oundnote is not installed. Run install-update.bat first.
    pause
    exit /b 1
)

if "%OUNDNOTE_PREPARED%"=="0" (
    "%OUNDNOTE_PYTHON%" -m local_meeting_ai.updater prepare --interactive --force
    set "OUNDNOTE_CHECK_CODE=!ERRORLEVEL!"
    if "!OUNDNOTE_CHECK_CODE!"=="0" (
        pause
        exit /b 0
    )
    if not "!OUNDNOTE_CHECK_CODE!"=="10" (
        echo.
        echo The update could not be prepared.
        pause
        exit /b !OUNDNOTE_CHECK_CODE!
    )
)

set "OUNDNOTE_RESTART_SWITCH="
if "%OUNDNOTE_RESTART%"=="1" set "OUNDNOTE_RESTART_SWITCH=-Restart"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\update.ps1" %OUNDNOTE_RESTART_SWITCH%
set "OUNDNOTE_UPDATE_CODE=%ERRORLEVEL%"

if not "%OUNDNOTE_UPDATE_CODE%"=="0" (
    echo.
    echo Oundnote was not updated. Existing data and settings were not modified.
    pause
    exit /b %OUNDNOTE_UPDATE_CODE%
)

if "%OUNDNOTE_RESTART%"=="0" pause
exit /b 0
