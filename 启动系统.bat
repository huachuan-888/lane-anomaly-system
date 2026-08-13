@echo off
chcp 65001 >nul
title Lane Anomaly Attribution System
echo ============================================
echo   Lane Anomaly Attribution System
echo ============================================
echo.
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [X] Python not found. Install Python 3.8+ first.
    pause
    exit /b
)

netstat -ano > "%TEMP%\ns.txt" 2>&1
findstr ":5000" "%TEMP%\ns.txt" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [i] Service already running. Opening browser...
    start "" http://127.0.0.1:5000/
    del "%TEMP%\ns.txt" >nul 2>&1
    pause
    exit /b
)
del "%TEMP%\ns.txt" >nul 2>&1

echo Starting local service...
echo Browser will open automatically. Close this window to stop.
echo.

python app.py

echo.
echo Service stopped.
pause