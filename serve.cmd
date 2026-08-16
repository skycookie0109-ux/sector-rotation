@echo off
REM ASCII only - see the note in update.cmd.
chcp 65001 >nul
cd /d "%~dp0web"

echo ============================================================
echo   Starting local web server on http://localhost:8749/
echo   Your browser will open automatically.
echo   Press Ctrl+C or close this window to stop.
echo ============================================================
echo.

start "" http://localhost:8749/
python -m http.server 8749
