@echo off
setlocal
chcp 65001 >nul
title Shuiyuan Incremental Report
cd /d "%~dp0"

if /i "%~1"=="--self-test" (
  echo SELFTEST_OK
  exit /b 0
)

echo ============================================================
echo   Shuiyuan incremental report
echo   Window: last successful run to now
echo   Diary and water threads are excluded
echo ============================================================
echo.

python "%~dp0shuiyuan_daily.py" --incremental --hours 2
set "exit_code=%errorlevel%"

echo.
if "%exit_code%"=="0" (
  echo Report completed. Opening the project folder.
  start "" explorer.exe "%~dp0"
) else (
  echo Report failed. If login expired, run the login BAT first.
)
echo.
pause
exit /b %exit_code%
