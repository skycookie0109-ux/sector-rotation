@echo off
REM ASCII only. cmd.exe reads batch files in the OEM codepage (cp950 here),
REM so any UTF-8 Chinese in this file gets mangled into bogus commands.
REM All Chinese output comes from the Python scripts instead.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ============================================================
echo   Taiwan Sector Rotation - Update Data
echo ============================================================
echo.

echo [1/4] Fetching market snapshots ...
python scripts\fetch.py
if errorlevel 1 goto fail
echo.

echo [2/4] Backfilling sector index history ...
python scripts\backfill.py --days 900
if errorlevel 1 goto fail
echo.

echo [3/4] Fetching institutional flows ...
python scripts\chips.py --days 20
if errorlevel 1 goto fail
echo.

echo [4/4] Computing rotation scores ...
python scripts\build.py
if errorlevel 1 goto fail
echo.

echo ============================================================
echo   Done. Run serve.cmd to open the dashboard.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo *** FAILED - see the error above. ***
pause
exit /b 1
