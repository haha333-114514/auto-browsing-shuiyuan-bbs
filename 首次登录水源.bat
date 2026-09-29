@echo off
setlocal
chcp 65001 >nul
title 水源社区 - 建立登录会话
cd /d "%~dp0"

echo 将打开一个水源专用 Edge 窗口。
echo 请手动完成交大统一身份认证；脚本不会读取或保存你的密码。
echo 登录成功进入水源后，程序会继续生成最近 24 小时日报。
echo.

python "%~dp0shuiyuan_daily.py" --hours 24 --login
set "exit_code=%errorlevel%"
echo.
pause
exit /b %exit_code%
