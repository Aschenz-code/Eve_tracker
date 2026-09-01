@echo off
rem The one file to launch. Everything else opens from here.
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" eve_watch.py hub
