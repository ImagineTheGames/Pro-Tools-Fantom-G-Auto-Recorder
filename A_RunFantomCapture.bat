@echo off
rem Start the stem capture console.
rem
rem Double-click this instead of right-clicking the .ps1. -ExecutionPolicy
rem Bypass applies to this one process only and changes nothing system-wide,
rem which is what "Run with PowerShell" refuses to do by default.

rem Run from the folder this file lives in, whatever the working directory was.
cd /d "%~dp0"

rem -NoExit is deliberately absent: the console closes when you quit with F10.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Fantom-Capture.ps1" %*

rem Only pause if it fell over, so a normal quit does not need a keypress.
if errorlevel 1 (
    echo.
    echo   Fantom-Capture exited with error %errorlevel%.
    pause
)
