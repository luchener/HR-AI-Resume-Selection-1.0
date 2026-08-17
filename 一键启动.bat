@echo off
setlocal

set "PS1_FILE=%~dp0start-resume-matcher.ps1"

if not exist "%PS1_FILE%" (
    echo start-resume-matcher.ps1 not found.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1_FILE%"
if errorlevel 1 (
    echo.
    echo Startup failed.
    pause
    exit /b 1
)

exit /b 0
