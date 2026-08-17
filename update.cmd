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

echo [1/8] Fetching market snapshots ...
python scripts\fetch.py
if errorlevel 1 goto fail
echo.

echo [2/8] Backfilling sector index history ...
python scripts\backfill.py --days 900
if errorlevel 1 goto fail
echo.

echo [3/8] Fetching institutional flows ...
python scripts\chips.py --days 20
if errorlevel 1 goto fail
echo.

echo [4/8] Updating financial history ...
python scripts\hist_fin.py --quarters 34
if errorlevel 1 goto fail
echo.

echo [5/8] Updating valuation history ...
python scripts\hist_val.py --months 96
if errorlevel 1 goto fail
echo.

echo [6/8] Computing rotation scores ...
python scripts\build.py
if errorlevel 1 goto fail
echo.

echo [7/8] Updating US market ...
python scripts\hist_us.py --quarters 32
if errorlevel 1 goto fail
python scripts\build_us.py
if errorlevel 1 goto fail
echo.

echo [8/8] Verifying data ...
python scripts\verify.py
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
