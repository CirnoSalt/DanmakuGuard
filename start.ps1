#Requires -Version 5.1
<#
    start.ps1 - DanmakuGuard 一键启动脚本（Windows / PowerShell）

    用法：
        双击运行，或在 PowerShell 中执行：  .\start.ps1
        跳过依赖检查与安装：               .\start.ps1 -SkipInstall

    若系统禁止运行脚本，用下面这条绕过（脚本内部无法自愈，被策略拦截时它根本不会执行）：
        powershell -ExecutionPolicy Bypass -File .\start.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall
)

# 重要：全局保持 Continue，不要用 Stop。
# PowerShell 5.1 会把「原生命令往 stderr 写内容」当成错误记录，
# 一旦设成 Stop，uvicorn / python 输出的第一条日志就会把启动脚本自己终止掉。
$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$ProjectDir   = $PSScriptRoot
$VenvDir      = Join-Path $ProjectDir '.venv'
$VenvActivate = Join-Path $VenvDir 'Scripts\Activate.ps1'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$AppEntry     = Join-Path $ProjectDir 'run.py'
$Requirements = Join-Path $ProjectDir 'requirements.txt'

# 避免中文提示在很多控制台里变乱码（部分宿主不支持，忽略即可）
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding           = [System.Text.Encoding]::UTF8
} catch { }

# --------------------------------------------------------------- 辅助函数
function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Message) Write-Host "[!] $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "[x] $Message" -ForegroundColor Red }

# 是否由资源管理器双击启动（是则结束时暂停，避免窗口一闪而过）
function Test-LaunchedFromExplorer {
    try {
        $self = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
        if ($null -eq $self) { return $false }
        $parent = Get-Process -Id $self.ParentProcessId -ErrorAction SilentlyContinue
        return ($null -ne $parent -and $parent.ProcessName -eq 'explorer')
    } catch {
        return $false
    }
}
$LaunchedFromExplorer = Test-LaunchedFromExplorer

function Wait-BeforeExit {
    param([int]$ExitCode = 0)
    if (-not $LaunchedFromExplorer) { exit $ExitCode }
    Write-Host ''
    Write-Host '按任意键关闭窗口...' -ForegroundColor DarkGray
    try {
        $null = [System.Console]::ReadKey($true)
    } catch {
        Start-Sleep -Seconds 3
    }
    exit $ExitCode
}

function Fail {
    param([string]$Message, [int]$ExitCode = 1)
    Write-Err $Message
    Wait-BeforeExit -ExitCode $ExitCode
}

# 统一调用虚拟环境里的 python：
# - 强制 Continue，防止 stderr 触发终止
# - 先把输出收进变量、立刻读 $LASTEXITCODE，再做字符串化。
#   注意：PS 5.1 里写成 `python -c xxx 2>&1 | ForEach-Object {...}` 会把退出码吞掉
#   （实测 exit 3 也会读成 0），所以这里绝不把原生命令直接塞进管道。
function Invoke-Python {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    $ErrorActionPreference = 'Continue'
    $script:PyExitCode = 1
    if (-not $Arguments -or $Arguments.Count -eq 0) { return @() }
    try {
        $raw = & $VenvPython @Arguments 2>&1
        $script:PyExitCode = $LASTEXITCODE
    } catch {
        $script:PyExitCode = 1
        $raw = $null
    }
    if ($null -eq $raw) { return @() }
    $lines = @()
    foreach ($item in @($raw)) { $lines += ([string]$item) }
    return $lines
}

function Test-ContainsMarker {
    param([string[]]$Lines, [string]$Marker)
    if (-not $Lines) { return $false }
    foreach ($line in $Lines) {
        if ($line -and $line.Trim() -eq $Marker) { return $true }
    }
    return $false
}

# 注意：传给 python 的代码片段里不能出现英文双引号！
# PowerShell 5.1 在把参数交给原生 exe 时会把双引号吃掉，
# 例如 print("VENV_OK") 会变成 print(VENV_OK) 而 SyntaxError。
# 统一用单引号（外层 PS 字符串用双引号包，内层 python 用单引号）。

# 虚拟环境「目录存在」不等于「可用」。
# 底层 Python 被卸载/重装后 pyvenv.cfg 仍指向旧路径，
# 此时 python.exe 只有 stderr、没有可用的退出码，
# 所以用「跑通并打印标记」来判断，不依赖退出码。
function Test-VenvUsable {
    if (-not (Test-Path -LiteralPath $VenvPython)) { return $false }
    $out = Invoke-Python '-c', "import sys; print('VENV_OK')"
    return (Test-ContainsMarker -Lines $out -Marker 'VENV_OK')
}

# 判断模块是否已安装：同样以标准输出为准，避免退出码不可靠
function Test-ModuleInstalled {
    param([string]$Module)
    $out = Invoke-Python '-c', "import importlib.util; print('MOD_OK' if importlib.util.find_spec('$Module') else 'MOD_MISSING')"
    return (Test-ContainsMarker -Lines $out -Marker 'MOD_OK')
}

function Get-PythonCommand {
    foreach ($name in @('python', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function New-Venv {
    Write-Step '创建虚拟环境 .venv'
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Note '使用 uv venv'
        & uv venv $VenvDir --allow-existing
        if ($LASTEXITCODE -ne 0) { Fail 'uv venv 创建失败。' }
        return
    }
    Write-Warn '未检测到 uv，回退使用 python -m venv'
    $sysPython = Get-PythonCommand
    if (-not $sysPython) {
        Fail '未找到 Python，请先安装 Python 3.10+ 并勾选 "Add python.exe to PATH"。'
    }
    & $sysPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Fail '创建虚拟环境失败（python -m venv）。' }
}

function Install-Dependencies {
    if (-not (Test-Path -LiteralPath $Requirements)) {
        Fail "缺少依赖清单：$Requirements"
    }
    Write-Step '安装依赖'
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Note '使用 uv pip install'
        & uv pip install -r $Requirements --python $VenvPython
        if ($LASTEXITCODE -ne 0) { Fail '依赖安装失败（uv pip）。' }
    } else {
        Write-Note '使用 pip install'
        & $VenvPython -m pip install -r $Requirements
        if ($LASTEXITCODE -ne 0) { Fail '依赖安装失败（pip）。' }
    }
}

# 从 config.yaml 读取监听地址，读不到就用默认值
# （同样只用单引号，见上方关于双引号被吞掉的说明）
function Get-ServerAddress {
    $probe = @'
import json
try:
    from app.config import load_settings
    s = load_settings('config.yaml')
    print(json.dumps({'host': s.server.host, 'port': int(s.server.port)}))
except Exception:
    print(json.dumps({'host': '127.0.0.1', 'port': 8000}))
'@
    try {
        $out = Invoke-Python '-c', $probe
        if ($out) {
            $raw = [string]($out | Select-Object -Last 1)
            $o = $raw | ConvertFrom-Json
            if ($o -and $o.port) {
                return [pscustomobject]@{ Host = [string]$o.host; Port = [int]$o.port }
            }
        }
    } catch { }
    return [pscustomobject]@{ Host = '127.0.0.1'; Port = 8000 }
}

# ----------------------------------------------------------- 1. 环境检查
Write-Step '环境检查'

if (-not (Test-Path -LiteralPath $AppEntry)) {
    Fail "找不到入口文件 run.py（当前目录：$ProjectDir），请确认 start.ps1 位于项目根目录。"
}

if (-not (Test-VenvUsable)) {
    if (Test-Path -LiteralPath $VenvDir) {
        Write-Warn '现有的 .venv 不可用（底层 Python 路径已失效），改名备份后重建'
        # 目录里残留的 pyvenv.cfg / 旧脚本会继续指向失效的解释器，
        # 单靠 uv venv --allow-existing 重建可能仍不可用，故先整体挪走再建。
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $brokenDir = "$VenvDir.broken-$stamp"
        try {
            Move-Item -LiteralPath $VenvDir -Destination $brokenDir -ErrorAction Stop
            Write-Note "旧环境已备份为：$brokenDir（确认新环境可用后可自行删除）"
        } catch {
            Write-Warn "旧环境无法改名（$($_.Exception.Message)），将尝试原地重建"
        }
    } else {
        Write-Note '未找到可用的虚拟环境，开始创建'
    }
    New-Venv
    if (-not (Test-VenvUsable)) {
        Fail "虚拟环境仍然不可用，请手动删除目录后重试：$VenvDir"
    }
}

Write-Note "虚拟环境：$VenvDir"
$verLine = Invoke-Python '-c', 'import sys; print(sys.version.split()[0])'
if ($verLine) {
    Write-Note ("解释器版本：Python " + [string]$verLine)
}

# 同时激活一下，让习惯直接用 python/pip 的后续命令也落到虚拟环境里
if (Test-Path -LiteralPath $VenvActivate) { . $VenvActivate }

# ----------------------------------------------------------- 2. 依赖检查
$RequiredModules = @('fastapi', 'uvicorn', 'httpx', 'openai', 'pydantic', 'yaml')

if ($SkipInstall) {
    Write-Step '已指定 -SkipInstall，跳过依赖检查'
} else {
    $missing = @()
    foreach ($m in $RequiredModules) {
        if (-not (Test-ModuleInstalled -Module $m)) { $missing += $m }
    }
    if ($missing.Count -gt 0) {
        Write-Warn "缺失依赖：$($missing -join ', ')"
        Install-Dependencies
        # 安装后再核验一次：装完仍缺说明装到了别的解释器或安装被中断，此处直接报错更省事
        $stillMissing = @()
        foreach ($m in $RequiredModules) {
            if (-not (Test-ModuleInstalled -Module $m)) { $stillMissing += $m }
        }
        if ($stillMissing.Count -gt 0) {
            Fail "依赖安装后仍缺失：$($stillMissing -join ', ')（请检查网络或手动执行 pip install -r requirements.txt）"
        }
        Write-Note '依赖安装完成'
    } else {
        Write-Note '依赖已就绪'
    }
}

# --------------------------------------------------- 3. 读取地址 & 端口冲突
$addr = Get-ServerAddress
$displayHost = if ($addr.Host -in @('0.0.0.0', '::')) { '127.0.0.1' } else { $addr.Host }
$serverUrl = "http://$($displayHost):$($addr.Port)"

try {
    $busy = Get-NetTCPConnection -LocalPort $addr.Port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        $busyPids = ($busy | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
        Write-Warn "端口 $($addr.Port) 已被占用（PID: $busyPids），可能已有实例在运行；若启动报错请先结束该进程。"
    }
} catch { }

# ------------------------------------------------------------- 4. 启动服务
Write-Step '启动 DanmakuGuard'
Write-Host "    访问地址：$serverUrl" -ForegroundColor Green
Write-Note '停止服务请按 Ctrl+C'

# 让可能产生的子 python 进程也复用同一个虚拟环境
$env:VIRTUAL_ENV = $VenvDir
$env:PATH = (Join-Path $VenvDir 'Scripts') + [IO.Path]::PathSeparator + $env:PATH

try {
    & $VenvPython $AppEntry
    $exitCode = $LASTEXITCODE
} catch {
    Write-Err "启动失败：$($_.Exception.Message)"
    Wait-BeforeExit -ExitCode 1
    return
}

if ($exitCode -ne 0) {
    Write-Err "服务异常退出，退出码：$exitCode"
    Wait-BeforeExit -ExitCode $exitCode
}

Wait-BeforeExit -ExitCode 0
