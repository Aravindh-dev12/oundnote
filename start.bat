@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "OUNDNOTE_PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%OUNDNOTE_PYTHON%" (
    echo.
    echo Oundnote is not installed yet.
    echo Run install.ps1 first from PowerShell:
    echo   Set-ExecutionPolicy -Scope Process Bypass
    echo   .\install.ps1
    echo.
    pause
    exit /b 1
)

echo.
echo Oundnote local server
echo The browser will not open automatically.
echo The exact local address will be shown below when the server is ready.
echo Press Ctrl+C in this window to stop Oundnote.
echo.

if /I not "%M2N_SKIP_UPDATE_CHECK%"=="1" (
    "%OUNDNOTE_PYTHON%" -m local_meeting_ai.updater check --interactive -- %*
    set "OUNDNOTE_UPDATE_CHECK_CODE=!ERRORLEVEL!"
    if "!OUNDNOTE_UPDATE_CHECK_CODE!"=="10" (
        echo Starting the safe updater in a separate window...
        start "Oundnote Update" cmd.exe /c ""%CD%\update.bat" --prepared --restart"
        exit /b 0
    )
)

"%OUNDNOTE_PYTHON%" -m local_meeting_ai --no-browser %*
set "OUNDNOTE_EXIT_CODE=%ERRORLEVEL%"

if not "%OUNDNOTE_EXIT_CODE%"=="0" (
    echo.
    echo Oundnote stopped with exit code %OUNDNOTE_EXIT_CODE%.
)

echo.
echo Oundnote has stopped. This window will stay open so you can read the log.
pause

exit /b %OUNDNOTE_EXIT_CODE%
