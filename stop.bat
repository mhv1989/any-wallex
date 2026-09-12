@echo off
cd /d "%~dp0"
python stop_all.py %*
pause
