@echo off
rem Starts the watcher in its own minimised console window.
rem Uses the bundled .venv so it does not matter which python is on PATH.
cd /d "%~dp0"
start "eve-watch" /min "%~dp0.venv\Scripts\python.exe" eve_watch.py watch %*
