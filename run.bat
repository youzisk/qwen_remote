@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
title Qwen Image Remote
"D:\qwen mou\ComfyUI_windows_portable\python_embeded\python.exe" server.py
echo.
echo Server stopped. You can close this window.
pause >nul
