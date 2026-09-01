@echo off
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" eve_watch.py dash
