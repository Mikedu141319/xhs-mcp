@echo off
chcp 65001 >nul
title 小红书登录助手 - Xiaohongshu Login Helper

echo.
echo ====================================================
echo    小红书登录助手 - Xiaohongshu Login Helper
echo ====================================================
echo.
echo 正在启动登录助手...
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

:: Try common Python locations
if exist "C:\Python311\python.exe" (
    C:\Python311\python.exe login_helper.py
    goto :end
)

if exist "C:\Python310\python.exe" (
    C:\Python310\python.exe login_helper.py
    goto :end
)

echo.
echo 错误：未找到 Python！
echo 请安装 Python 3.10+ 后重试。
echo 下载地址: https://www.python.org/downloads/
echo.
pause

:end
