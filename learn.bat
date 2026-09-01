@echo off
cd /d "%~dp0"
echo.
set /p VAL=Number shown in the structure bracket right now: 
"%~dp0.venv\Scripts\python.exe" eve_watch.py learn --name structure --value %VAL%
echo.
echo The running watcher picks this up within a few seconds - no restart needed.
pause
