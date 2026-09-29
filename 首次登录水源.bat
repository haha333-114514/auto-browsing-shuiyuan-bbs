@echo off
setlocal
chcp 65001 >nul
title Shuiyuan Login
cd /d "%~dp0"

if /i "%~1"=="--self-test" (
  echo SELFTEST_OK
  exit /b 0
)

echo A dedicated Microsoft Edge window will open.
echo Complete SJTU authentication in that window.
echo This only saves the login session; it will not collect topics.
echo The script never reads or exports your password.
echo.

python "%~dp0shuiyuan_daily.py" --login-only
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
