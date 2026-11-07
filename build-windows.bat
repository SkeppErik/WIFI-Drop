@echo off
rem Builds dist\WiFiDrop.exe on a Windows PC. Needs Python 3.10+ from python.org.
cd /d "%~dp0"
py -3 build.py || python build.py
if errorlevel 1 (
  echo.
  echo Build failed. Install Python from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
echo.
echo Done: dist\WiFiDrop.exe
pause
