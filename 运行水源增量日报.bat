@echo off
setlocal
chcp 65001 >nul
title Shuiyuan Incremental Report
cd /d "%~dp0"

if /i "%~1"=="--self-test" (
  echo SELFTEST_OK
  exit /b 0
)

if /i "%~1"=="--scheduled" goto scheduled

echo ============================================================
echo   Shuiyuan incremental report
echo   Window: last successful run to now
echo   Unwanted categories and diary-like topics are excluded
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

:scheduled
if not exist "%~dp0outputs" mkdir "%~dp0outputs"
set "python_cmd=python"
if not "%~2"=="" set "python_cmd=%~2"
echo [%date% %time%] Scheduled incremental run started.>>"%~dp0outputs\scheduled_run.log"
"%python_cmd%" -u "%~dp0shuiyuan_daily.py" --incremental --hours 2 --no-interactive >>"%~dp0outputs\scheduled_run.log" 2>&1
set "exit_code=%errorlevel%"
echo [%date% %time%] Exit code: %exit_code% >>"%~dp0outputs\scheduled_run.log"
exit /b %exit_code%
