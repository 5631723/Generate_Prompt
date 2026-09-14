@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python ui_pyside6.py
) else (
    py ui_pyside6.py
)
if errorlevel 1 pause
