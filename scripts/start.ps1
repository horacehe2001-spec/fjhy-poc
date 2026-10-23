# 福建海运集团POC · 一键启动
# ============================================================================
# 做四件事：起数据库 → 起后端 → 起前端 → 开浏览器。
# 服务已经在跑的话先停掉再起。
#
# 【最要紧的一条：不见端口就杀】
#   「占着端口的进程」不等于「本项目的进程」。本机 8000 端口上就有一个
#   跟本项目无关的服务（别的工程）。盲目 taskkill 会打掉用户正在用的东西。
#   所以停服务一律先核对进程的命令行：含 `uvicorn app.main` 且工作目录在本项目
#   才认；不认的一律不碰，改用备用端口并明确告知。
#
# 用法：
#   双击 启动.bat
#   或   powershell -ExecutionPolicy Bypass -File scripts\start.ps1
#   参数：-ApiPort 8010 -WebPort 5174 -NoBrowser -Reset
# ============================================================================
param(
  [int]$ApiPort = 8000,
  [int]$WebPort = 5173,
  [switch]$NoBrowser,
  [switch]$Reset,          # 重建数据库（会丢数据，需二次确认）
  [switch]$StopOnly        # 只停服务，不启动
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

# Python 子进程往管道里写时默认用系统 ANSI 编码，中文会变乱码。
# 实测：migrate.py 的输出在脚本里显示成「������״��…」。
# 设成 utf-8 之后才看得懂——看不懂的输出等于没有输出。
# 注意：下面调 Python 一律用 `python -X utf8 -I`，不是 `python -I`。
# `-I`（隔离模式）隐含 `-E`，会**忽略所有 PYTHON* 环境变量**——
# 所以设 PYTHONIOENCODING 没用，实测 sys.stdout.encoding 仍是 gbk，
# 输出在这里显示成「������״��…」。看不懂的输出等于没有输出。
# `-X utf8` 是命令行开关，隔离模式下照样生效。
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

$Root     = Split-Path -Parent $PSScriptRoot
$Server   = Join-Path $Root '服务端'
$Web      = Join-Path $Root '前端'
$LogDir   = Join-Path $Root 'var\log'
$null     = New-Item -ItemType Directory -Force -Path $LogDir

function Say($msg, $color = 'Gray')  { Write-Host $msg -ForegroundColor $color }
function Step($msg)                  { Write-Host "`n[$([DateTime]::Now.ToString('HH:mm:ss'))] $msg" -ForegroundColor Cyan }
function Ok($msg)                    { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg)                  { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Die($msg) {
  Write-Host "`n  XX  $msg" -ForegroundColor Red
  # 双击运行时停下来等按键，好让人看完报错。
  # 但输入被重定向（CI、`< NUL`、被别的脚本调用）时 ReadKey 不会报错，
  # 而是**无限等下去**——实测挂了 7 分钟，看着像启动很慢，其实早就失败了。
  if (-not [Console]::IsInputRedirected) {
    Write-Host "`n按任意键退出…" -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
  }
  exit 1
}

# 日志按次归档：失败时要看的是「这一次」的输出。
# 上一版直接续写，于是报错里打出来的「最后 25 行」是上次运行留下的，
# 看着像服务在跑、实际这次一行没写——假日志比没日志更坏。
function Reset-Log($path) {
  if (-not (Test-Path $path)) { return }
  try { Move-Item $path "$path.prev" -Force -ErrorAction Stop }
  catch { try { Clear-Content $path -ErrorAction Stop } catch { } }
}

# ── 进程归属判断 ────────────────────────────────────────────────────────────
# 两条路identify本项目的进程，缺一不可：
#
#   A. PID 文件。启动时把实际监听端口的那个 PID 记到 var/run/ 下。
#      这是**最准的一条**——不靠字符串猜。
#   B. 命令行匹配。捡 PID 文件丢失或进程重启后的漏网之鱼。
#      必须同时含「服务标记」和「本项目路径」：
#      只看 vite 会杀掉别的前端工程；只看项目路径会杀掉正在编辑本项目的编辑器。
#
# **还必须杀进程树**。后端用 cmd.exe 包了一层（为了 tee 日志），
# 而 Windows 上杀父进程不会级联杀子进程——实测踩到：停止脚本报「已停止」，
# 可 8001 端口上的 python.exe 还活着。假成功比失败更坏。
$RunDir = Join-Path (Join-Path $Root 'var') 'run'
$null = New-Item -ItemType Directory -Force -Path $RunDir

function Save-Pid($kind, $processId) {
  Set-Content -Path (Join-Path $RunDir "$kind.pid") -Value $processId -Encoding ascii
}

function Read-Pid($kind) {
  $f = Join-Path $RunDir "$kind.pid"
  if (-not (Test-Path $f)) { return $null }
  $v = (Get-Content $f -Raw).Trim()
  if ($v -match '^\d+$') { return [int]$v }
  return $null
}

function Get-Descendants($processId) {
  # 递归找子孙进程。cmd.exe -> python.exe 这种两层结构，只杀父的话子进程会留下。
  $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$processId" -ErrorAction SilentlyContinue
  $out = @()
  foreach ($k in $kids) {
    $out += $k.ProcessId
    $out += Get-Descendants $k.ProcessId
  }
  return $out
}

function Stop-Tree($processId, $why) {
  if (-not $processId) { return 0 }
  if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return 0 }
  $all = @($processId) + (Get-Descendants $processId)
  $n = 0
  # 先杀子孙再杀父：反过来的话父进程一死，子进程的 ParentProcessId 就指向不存在的
  # PID，Get-Descendants 再也找不到它们。
  foreach ($id in ($all | Select-Object -Unique | Sort-Object -Descending)) {
    try {
      Stop-Process -Id $id -Force -ErrorAction Stop
      $n++
    } catch { }
  }
  if ($n) { Say "  停掉 $why（$n 个进程，根 PID $processId）" }
  return $n
}

function Get-OurProcesses($kind) {
  $mark = switch ($kind) {
    'api' { 'uvicorn' }
    'web' { 'vite' }
  }
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and
    $_.CommandLine -like "*$mark*" -and
    $_.CommandLine -like "*福建海运集团POC*"
  }
}

function Get-PortHolder($port) {
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
       Select-Object -First 1
  if (-not $c) { return $null }
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
  if (-not $p) { return $null }
  [pscustomobject]@{ Pid = $c.OwningProcess; Name = $p.Name; CommandLine = $p.CommandLine }
}

function Stop-Ours($kind, $label) {
  $killed = 0
  # A. 先按 PID 文件停（最准）
  $killed += Stop-Tree (Read-Pid $kind) $label
  # B. 再按命令行捡漏网的
  foreach ($p in @(Get-OurProcesses $kind)) {
    $killed += Stop-Tree $p.ProcessId $label
  }
  Remove-Item (Join-Path $RunDir "$kind.pid") -ErrorAction SilentlyContinue
  if ($killed -eq 0) { Say "  $label 没有在跑" }
  Start-Sleep -Milliseconds 800
  return $killed
}

# 记下启动时实际监听端口的那个进程（可能是 cmd.exe 的子进程，不是我们直接起的那个）
function Record-Listener($kind, $port) {
  $h = Get-PortHolder $port
  if ($h) { Save-Pid $kind $h.Pid; return $h.Pid }
  return $null
}

# 端口可用性：被本项目占 → 已在上一步停掉；被别人占 → 换端口，**绝不杀**
function Resolve-Port($want, $label, $kind) {
  for ($p = $want; $p -lt ($want + 20); $p++) {
    $h = Get-PortHolder $p
    if (-not $h) {
      if ($p -ne $want) { Warn "$label 端口 $want 被占，改用 $p" }
      return $p
    }
    $mine = $h.CommandLine -and $h.CommandLine -like "*福建海运集团POC*"
    if ($mine) {
      Say "  端口 $p 上是本项目的残留进程（PID $($h.Pid)），停掉"
      try { Stop-Process -Id $h.Pid -Force } catch { }
      Start-Sleep -Milliseconds 700
      if (-not (Get-PortHolder $p)) { return $p }
    } else {
      Warn "$label 端口 $p 被**别的程序**占用（PID $($h.Pid) $($h.Name)），不动它，试下一个端口"
    }
  }
  Die "$label 从 $want 起连续 20 个端口都不可用"
}

function Wait-Url($url, $label, $seconds = 60) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
      if ($r.StatusCode -eq 200) { return $true }
    } catch { }
    Start-Sleep -Milliseconds 700
  }
  return $false
}

# ============================================================================
Write-Host ''
Write-Host '福建海运集团POC · 差旅费 AI 智能审核' -ForegroundColor White
Write-Host '--------------------------------------------------------------' -ForegroundColor DarkGray

# ── 0 停掉已在跑的本项目服务 ────────────────────────────────────────────────
Step '停掉已在跑的服务（只停本项目的）'
$null = Stop-Ours 'api' '后端'
$null = Stop-Ours 'web' '前端'

if ($StopOnly) {
  # **停完必须验证**。第一版停完就报「已停止」，可 8001 端口上的 python.exe
  # 还活着——因为后端被 cmd.exe 包了一层，杀父不杀子。
  # 报了假成功，下次启动时端口冲突，而人会以为是别的原因。
  Start-Sleep -Milliseconds 600
  $left = @()
  foreach ($p in @($ApiPort, $WebPort, 8001, 5174)) {
    $h = Get-PortHolder $p
    if ($h -and $h.CommandLine -like "*福建海运集团POC*") { $left += "$p (PID $($h.Pid))" }
  }
  if ($left.Count) {
    Warn "这些端口上还有本项目的进程没停干净：$($left -join ', ')"
    Warn '再跑一次 停止.bat，或手动 taskkill'
    exit 1
  }
  Ok '已停止，端口已释放。数据库容器未动（docker compose stop 可停）。'
  exit 0
}

# ── 1 数据库 ────────────────────────────────────────────────────────────────
Step '数据库容器 fjhy-poc-sdafi-pg'
try { docker version --format '{{.Server.Version}}' > $null 2>&1 } catch { Die 'Docker 没装或没启动' }
if ($LASTEXITCODE -ne 0) { Die 'Docker Desktop 没启动，先把它起来' }

$state = (docker inspect -f '{{.State.Status}}' fjhy-poc-sdafi-pg 2>$null)
if ($LASTEXITCODE -ne 0) {
  Say '  容器不存在，创建中…'
  Push-Location $Root
  docker compose up -d | Out-Null
  Pop-Location
} elseif ($state -ne 'running') {
  Say "  容器状态 $state，启动中…"
  docker start fjhy-poc-sdafi-pg | Out-Null
} else {
  Say '  容器已在运行'
}

Say '  等待数据库就绪…'
$ready = $false
foreach ($i in 1..40) {
  docker exec fjhy-poc-sdafi-pg pg_isready -U fjhy -d fjhy_poc > $null 2>&1
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Start-Sleep -Milliseconds 800
}
if (-not $ready) { Die '数据库 40 秒内没就绪，看 docker logs fjhy-poc-sdafi-pg' }
Ok '数据库就绪（127.0.0.1:55460）'

# ── 2 初始化（只在需要时做）─────────────────────────────────────────────────
Push-Location $Server
if ($Reset) {
  Warn '即将重建数据库，所有数据会丢。输入 yes 继续：'
  if ((Read-Host) -ne 'yes') { Pop-Location; Die '已取消' }
  $env:FJHY_RESET_CONFIRM = 'yes'
  python -X utf8 -I db/migrate.py --reset | Out-Null
  Remove-Item Env:\FJHY_RESET_CONFIRM
}

Step '数据库结构与规则资产'
# stderr 交给 cmd 合并，**不要**在 PowerShell 里写 `python ... 2>&1`：
# PS 5.1 会把 native 命令的每行 stderr 包成 ErrorRecord（NativeCommandError），
# 配合本脚本开头的 $ErrorActionPreference='Stop'，哪怕 python 退出码是 0
# 也会把整个启动流程打断。
cmd /c "python -X utf8 -I db/migrate.py 2>&1" | Select-Object -Last 2 | ForEach-Object { Say "  $_" }
if ($LASTEXITCODE -ne 0) { Pop-Location; Die '迁移失败' }

# 规则为空才装载——装载是幂等的，但每次重跑要十几秒，没必要
$ruleCount = (python -X utf8 -I -c "import sys,os;sys.path.insert(0,os.getcwd());from app import db;print(db.scalar('SELECT count(*) FROM rule') or 0)" 2>$null)
if ([int]$ruleCount -eq 0) {
  Say '  规则资产为空，装载中…'
  python -X utf8 -I app/ontology/loader.py | Out-Null
  python -X utf8 -I app/rules/loader.py    | Out-Null
  Ok '本体与规则资产已装载'
} else { Say "  规则资产已在库（$ruleCount 条）" }

$billCount = (python -X utf8 -I -c "import sys,os;sys.path.insert(0,os.getcwd());from app import db;print(db.scalar('SELECT count(*) FROM expense_bill') or 0)" 2>$null)
if ([int]$billCount -eq 0) {
  Say '  业务数据为空，装载样本与演示数据中…'
  python -X utf8 -I app/demo/seed.py      | Out-Null
  python -X utf8 -I app/demo/generator.py | Out-Null
  Say '  跑一轮审核…'
  python -X utf8 -I -c "import sys,os;sys.path.insert(0,os.getcwd());from app.runtime.orchestrator import Orchestrator;Orchestrator(verbose=False).startup().heartbeat_once(limit=300)" | Out-Null
  Ok '样本与演示数据已就绪'
} else { Say "  业务数据已在库（$billCount 张单）" }
Pop-Location

# ── 3 端口 ──────────────────────────────────────────────────────────────────
Step '分配端口'
$ApiPort = Resolve-Port $ApiPort '后端' 'api'
$WebPort = Resolve-Port $WebPort '前端' 'web'
Ok "后端 $ApiPort · 前端 $WebPort"

# ── 4 后端 ──────────────────────────────────────────────────────────────────
Step "启动后端（:$ApiPort）"
$apiLog = Join-Path $LogDir 'backend.log'
Reset-Log $apiLog
# **不要用 `| tee`**。Windows 的 cmd 没有 tee，实测报 `'tee' is not recognized`——
# 管道右端起不来，uvicorn 跟着起不来，日志也一个字写不出来，
# 于是失败时打出来的是上一次的旧日志，排查方向整个被带偏。
# （tee.exe 只在 C:\Program Files\Git\usr\bin 下，不在 PATH 上，不能指望它。）
# 改用 cmd 自己的重定向：不依赖任何外部命令。
Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/c', "chcp 65001 >nul && title FJHY 后端 :$ApiPort && python -X utf8 -I -m uvicorn app.main:app --host 127.0.0.1 --port $ApiPort > `"$apiLog`" 2>&1" `
  -WorkingDirectory $Server -WindowStyle Minimized
if (-not (Wait-Url "http://127.0.0.1:$ApiPort/health" '后端' 60)) {
  Say ''
  Say '  后端最后 25 行日志：' 'DarkGray'
  # 日志是 UTF-8（cmd 侧 chcp 65001），PS 5.1 的 Get-Content 默认按 ANSI 读，中文会乱码
  if (Test-Path $apiLog) { Get-Content $apiLog -Tail 25 -Encoding UTF8 | ForEach-Object { Say "    $_" 'DarkGray' } }
  Die "后端 60 秒内没起来。常见原因：启动时契约校验没过（Fail Fast，这是故意的）。日志：$apiLog"
}
$apiPid = Record-Listener 'api' $ApiPort
Ok "后端就绪 http://127.0.0.1:$ApiPort（PID $apiPid）"

# ── 5 前端 ──────────────────────────────────────────────────────────────────
Step "启动前端（:$WebPort）"
if (-not (Test-Path (Join-Path $Web 'node_modules'))) {
  Say '  node_modules 不在，npm install 中（首次会慢）…'
  Push-Location $Web; npm install --no-audit --no-fund | Out-Null; Pop-Location
}
$webLog = Join-Path $LogDir 'frontend.log'
Reset-Log $webLog
# 同上：这里原来也是 `| tee`，一样起不来。
Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/c', "chcp 65001 >nul && title FJHY 前端 :$WebPort && set FJHY_API_TARGET=http://127.0.0.1:$ApiPort&& set FJHY_WEB_PORT=$WebPort&& npx vite --port $WebPort > `"$webLog`" 2>&1" `
  -WorkingDirectory $Web -WindowStyle Minimized
if (-not (Wait-Url "http://127.0.0.1:$WebPort/" '前端' 90)) {
  if (Test-Path $webLog) { Get-Content $webLog -Tail 25 -Encoding UTF8 | ForEach-Object { Say "    $_" 'DarkGray' } }
  Die "前端 90 秒内没起来。日志：$webLog"
}
$webPid = Record-Listener 'web' $WebPort
Ok "前端就绪 http://127.0.0.1:$WebPort（PID $webPid）"

# ── 6 自检：确认跑起来的是能用的 ────────────────────────────────────────────
# 「端口通了」不等于「能用」。这里真调一次接口，顺带把关键数字打出来——
# 没有这一步，一个契约校验没过、规则一条都没装的后端也会显示「就绪」。
Step '自检'
try {
  $sum = Invoke-RestMethod "http://127.0.0.1:$WebPort/api/asset/rules/summary" -TimeoutSec 15
  Ok "规则 $($sum.total) 条 · 可编译 $($sum.compiled) · 执行集 $($sum.executable)"
  $hc  = Invoke-RestMethod "http://127.0.0.1:$WebPort/health" -TimeoutSec 15
  $audit = if ($hc.audit.problems.Count -eq 0) { '一致' } else { "有 $($hc.audit.problems.Count) 项问题" }
  Ok "数据库 $($hc.db.tables) 表 · 迁移 $($hc.db.migrations) · 脱敏 $($hc.mask_mode) · 审计$audit"
  $bills = Invoke-RestMethod "http://127.0.0.1:$WebPort/api/bills?size=1" -TimeoutSec 15
  Ok "单据 $($bills.total) 张（真实 $($bills.banner.real_bills) · 演示 $($bills.banner.demo_bills)）"
} catch {
  Warn "自检接口调用失败：$_"
  Warn '服务起来了但可能不可用，打开页面后留意报错'
}

# ── 7 打开浏览器 ────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '--------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host "  前端  http://127.0.0.1:$WebPort" -ForegroundColor White
Write-Host "  后端  http://127.0.0.1:$ApiPort/docs  （接口文档）" -ForegroundColor White
Write-Host "  日志  $LogDir" -ForegroundColor DarkGray
Write-Host "  停止  双击 停止.bat" -ForegroundColor DarkGray
Write-Host '--------------------------------------------------------------' -ForegroundColor DarkGray

if (-not $NoBrowser) {
  Start-Process "http://127.0.0.1:$WebPort/"
  Say '已打开浏览器' 'Green'
}
Write-Host ''
