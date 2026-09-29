@echo off
setlocal
chcp 65001 >nul
title 水源社区增量日报
cd /d "%~dp0"

echo ============================================================
echo   水源社区增量日报
echo   统计上一次成功运行之后新增或编辑楼层的主题
echo   日记、打卡、投喂及灌水楼会自动排除
echo ============================================================
echo.

python "%~dp0shuiyuan_daily.py" --incremental --hours 2
set "exit_code=%errorlevel%"

echo.
if "%exit_code%"=="0" (
  echo 增量日报生成完成。
  if exist "%~dp0水源日报输出" start "" explorer.exe "%~dp0水源日报输出"
) else (
  echo 生成失败。若提示登录过期，请先双击“首次登录水源.bat”。
)
echo.
pause
exit /b %exit_code%
