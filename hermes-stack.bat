@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0hermes-stack.ps1" %*
exit /b %errorlevel%
