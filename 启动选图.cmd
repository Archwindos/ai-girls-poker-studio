@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp004-制作工具\启动选图.ps1"
if errorlevel 1 pause
