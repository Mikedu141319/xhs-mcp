@echo off
chcp 65001 >nul
title Xiaohongshu Login Helper

echo.
echo ====================================================
echo    Xiaohongshu Login Helper
echo ====================================================
echo.
echo Starting login helper...
echo.

cd /d "%~dp0"

:: Try to use Python from PATH first
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python login_helper.py
    goto :end
)

:: Try py launcher
py --version >nul 2>&1
if %errorlevel% equ 0 (
    py login_helper.py
    goto :end
)

echo.
echo Error: Python not found!
echo Please install Python 3.10+ and try again.
echo Download: https://www.python.org/downloads/
echo.
pause

:end
