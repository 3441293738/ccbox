@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0CCBOX.exe" (
  start "" "%~dp0CCBOX.exe"
  exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0ccbox.py"
  if errorlevel 1 pause
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0ccbox.py"
  if errorlevel 1 pause
  exit /b
)
echo CCBOX.exe was not found. Install Python 3.11+ with Tcl/Tk, or use the complete portable package.
pause
exit /b 1
