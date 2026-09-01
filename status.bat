@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" eve_watch.py status
pause
