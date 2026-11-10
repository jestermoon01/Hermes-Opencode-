@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-hermes-opencode.ps1" %*
exit /b %errorlevel%
