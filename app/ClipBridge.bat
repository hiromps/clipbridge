@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_clipbridge.ps1"
if errorlevel 1 (
  echo ClipBridge could not start. Check the message above.
  pause
)
