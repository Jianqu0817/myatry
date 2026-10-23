@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
uv sync --frozen --python 3.12
if errorlevel 1 exit /b %errorlevel%
"%~dp0.venv\Scripts\python.exe" -m myatry prepare
if errorlevel 1 exit /b %errorlevel%
"%~dp0.venv\Scripts\python.exe" -m myatry check
