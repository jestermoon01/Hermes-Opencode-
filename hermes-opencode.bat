@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0hermes-stack.ps1" -OpenCode %*
exit /b %errorlevel%
