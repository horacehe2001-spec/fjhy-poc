@echo off
rem ===========================================================================
rem 【改这个文件前必读：换行必须是 CRLF，不能存成 LF】
rem   cmd.exe 读 .bat 是按 CRLF 边界做字节定位的。存成 Unix LF 之后它会错位，
rem   症状是命令被吃掉头一个字符、中文 rem 行中途炸成新命令：
rem     set "ARGS=" -> 'et' is not recognized
rem     -ExecutionPolicy -> 'nPolicy' is not recognized
rem   于是下面 powershell 那行根本没执行，服务一个也起不来，而双击时窗口一闪就没了，
rem   看着像「批处理不好用」。2026-10-20 实测踩到并修复。
rem   自查： powershell -c "$b=[IO.File]::ReadAllBytes('启动.bat'); ($b|? {$_-eq13}).Count"
rem   结果为 0 就是坏的。
rem
rem 停掉本项目的前后端服务。
rem
rem **只停本项目的进程**，判据是命令行里同时含 uvicorn/vite 和本项目路径。
rem 不按端口杀：占着端口的未必是本项目的东西。
rem
rem 数据库容器不动——它起停慢，而且里面有装载好的数据。
rem 真要停：docker compose stop
rem ===========================================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -StopOnly
echo.
pause
