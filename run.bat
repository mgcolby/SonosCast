@echo off
rem Dev launcher: run SonosCast from the venv without a console window.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0sonoscast.py"
