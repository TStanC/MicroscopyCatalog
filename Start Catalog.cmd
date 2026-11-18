@echo off
rem Starts the microscopy catalog server (if it is not running yet) and opens the page.
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Not installed yet - run:  powershell -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0serve_catalog.py" --open
