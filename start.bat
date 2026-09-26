@echo off
chcp 65001 >nul
title ChatGPT Desktop Bridge

echo ========================================================
echo   ChatGPT Desktop Bridge (CDP / OpenAI API Adapter)
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [!] Python is not installed or not in PATH.
    pause
    exit /b 1
)

python -m pip install -r requirements.txt -q
python bridge.py %*

if %errorlevel% neq 0 (
    pause
)
