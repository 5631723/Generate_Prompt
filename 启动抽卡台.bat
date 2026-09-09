@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python ui.py
) else (
    py ui.py
)
if errorlevel 1 pause
