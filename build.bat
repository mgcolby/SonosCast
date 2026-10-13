@echo off
rem Build a single-file SonosCast.exe into dist\ and zip it with the README for sharing.
cd /d "%~dp0"
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name SonosCast sonoscast.py
if errorlevel 1 exit /b 1
copy /y README.md dist\README.md >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\SonosCast.exe, dist\README.md -DestinationPath dist\SonosCast-share.zip"
echo Built dist\SonosCast.exe and dist\SonosCast-share.zip
