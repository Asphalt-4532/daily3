@echo off
REM Double-click this to run the installer. It just launches install.ps1
REM with the execution-policy restriction bypassed for this one run only
REM (this doesn't change any system-wide PowerShell setting).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
