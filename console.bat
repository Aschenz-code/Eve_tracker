@echo off
rem Opens a prompt where "python" is this tool's bundled interpreter.
cd /d "%~dp0"
set "PATH=%~dp0.venv\Scripts;%PATH%"
echo.
echo eve-watch console - you are in %CD%
echo.
echo   python eve_watch.py windows
echo   python eve_watch.py status
echo   python eve_watch.py shot --client "Name"
echo   python eve_watch.py calibrate --client "Name"
echo.
cmd /k
