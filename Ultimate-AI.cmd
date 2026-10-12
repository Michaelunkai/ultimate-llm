@echo off
rem PATH-proof launcher: absolute PowerShell path, bare-name fallback
set "UAI_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%UAI_PS%" set "UAI_PS=powershell"
"%UAI_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-UltimateAI.ps1" %*
if errorlevel 1 pause
