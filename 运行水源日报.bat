@echo off
setlocal
chcp 65001 >nul
title 水源社区 24 小时日报
cd /d "%~dp0"

echo ============================================================
echo   水源社区日报：统计启动时刻向前 24 小时
echo   直连水源，不使用系统代理；输出保存在“水源日报输出”文件夹
echo ============================================================
echo.

python "%~dp0shuiyuan_daily.py" --hours 24
set "exit_code=%errorlevel%"

echo.
if "%exit_code%"=="0" (
  echo 日报生成完成。
  if exist "%~dp0水源日报输出" start "" explorer.exe "%~dp0水源日报输出"
) else (
  echo 生成失败。若提示登录过期，请先双击“首次登录水源.bat”。
)
echo.
pause
exit /b %exit_code%
