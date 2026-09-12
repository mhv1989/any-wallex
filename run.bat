@echo off
cd /d "%~dp0"
python launcher.py
if errorlevel 1 pause
