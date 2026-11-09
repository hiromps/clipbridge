@echo off
setlocal
rem ClipBridge launcher: starts the server hidden and opens the PC setup page.
rem Requires Python 3.10+ (python.org or Microsoft Store).
set "PYW="
for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"
if not defined PYW for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"
if not defined PYW (
  echo Python 3 was not found.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/ or the Microsoft Store,
  echo then double-click ClipBridge.bat again.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0server.py"
exit /b 0
