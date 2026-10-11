@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-UltimateAI.ps1" %*
if errorlevel 1 pause
