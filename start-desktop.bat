@echo off
setlocal
cd /d "%~dp0"
set "OUNDNOTE_PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%OUNDNOTE_PYTHON%" (
  echo Oundnote is not installed yet. Run install.ps1 first.
  pause
  exit /b 1
)
"%OUNDNOTE_PYTHON%" -m local_meeting_ai.desktop %*
exit /b %ERRORLEVEL%
