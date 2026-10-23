@echo off
chcp 65001 >nul
title 福建海运集团POC 启动
rem ===========================================================================
rem 福建海运集团POC  差旅费 AI 智能审核  一键启动
rem
rem 双击即可。依次做四件事：起数据库，起后端，起前端，开浏览器。
rem 本项目的服务已经在跑的话，会先停掉旧进程再起。
rem 真正的逻辑在 scripts\start.ps1，这个文件只是个双击入口。
rem 停服务：双击 停止.bat
rem
rem 用法：
rem   启动.bat                默认 后端 8000，前端 5173
rem   启动.bat 8010 5174      两个数字分别当 后端端口、前端端口
rem   启动.bat -NoBrowser     以减号开头的参数，原样转给 start.ps1
rem   启动.bat -Reset         重建数据库，会丢数据，脚本里有二次确认
rem   启动.bat /?             只看用法，不启动
rem ===========================================================================
rem 【改这个文件前必读，下面三条都是实测踩过的坑】
rem
rem 1) 换行必须是 CRLF，不能存成 LF。
rem    cmd.exe 读 .bat 是按 CRLF 边界做字节定位的，存成 Unix LF 之后会错位：
rem    命令被吃掉头一个字符（set "ARGS=" 变成 et 找不到命令），
rem    中文 rem 行中途炸成新命令，于是下面调 powershell 那行根本没执行，
rem    服务一个也起不来；而双击时窗口一闪就没了，看着像批处理不好用。
rem    自查：用 PowerShell 数这个文件里字节 13 有多少个，结果是 0 就是坏的。
rem
rem 2) 注释里不要出现 大于号、竖线、与号 这三个字符。
rem    它们在 cmd 里是重定向和连接符。一旦第 1 条的错位发生，rem 就保护不住，
rem    注释的右半句会被当成文件名建出空文件来。
rem    2026-10-21 实测到的现象：项目根目录冒出 原样转给、当端口 两个 0 字节文件，
rem    文件名正是上一版注释里箭头右边的字。成因在本机没能复现（当前 cmd 的 rem
rem    会吞掉重定向），所以是推断不是定论；但注释里回避这三个字符是便宜的保险，
rem    所以这一版一律写中文的「大于」「小于」，不画箭头。
rem
rem 3) 拼 ARGS 的那几行不要塞进 if 的括号块，也不要写「if 条件 set ... 连接符 goto」。
rem    括号块内的变量在解析整块时就一次性展开完了，后一行会把前一行的结果覆盖掉；
rem    而连接符不归 if 管，goto 会无条件执行，后面的端口赋值永远到不了。
rem    实测症状：启动.bat 8010 5174 仍然跑在 8000。所以一律用显式 label 分支。
rem ===========================================================================
cd /d "%~dp0"

if not exist "%~dp0scripts\start.ps1" (
  echo.
  echo 找不到 scripts\start.ps1 。
  echo 这个 .bat 必须和 scripts 目录放在同一层，不要单独拷走。
  echo 当前位置："%~dp0"
  echo.
  pause
  exit /b 1
)

set "ARGS="
set "A1=%~1"
if not defined A1 goto :run
if "%A1%"=="/?" goto :help
if "%A1%"=="-?" goto :help
if not "%A1:~0,1%"=="-" goto :ports
set "ARGS= %*"
goto :run

:ports
set "ARGS= -ApiPort %~1"
if not "%~2"=="" set "ARGS=%ARGS% -WebPort %~2"
if not "%~3"=="" set "ARGS=%ARGS% %~3"
if not "%~4"=="" set "ARGS=%ARGS% %~4"

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"%ARGS%
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo 启动失败。原因在上面那段输出里，详细日志：
echo   后端  var\log\backend.log
echo   前端  var\log\frontend.log
echo.
echo 几个常见原因：
echo   - Docker Desktop 没启动，数据库容器起不来
echo   - 后端启动时契约校验没过（这是故意的 Fail Fast，日志里有具体哪一条）
echo   - 找不到 powershell 或 python，PATH 里没有
echo.
pause
exit /b 1

:help
echo.
echo 福建海运集团POC  一键启动
echo.
echo   启动.bat                默认 后端 8000，前端 5173
echo   启动.bat 8010 5174      指定 后端端口、前端端口
echo   启动.bat -NoBrowser     起服务，但不开浏览器
echo   启动.bat -Reset         重建数据库，会丢数据，有二次确认
echo.
echo 停服务：双击 停止.bat （只停本项目的进程，数据库容器不动）
echo.
exit /b 0
