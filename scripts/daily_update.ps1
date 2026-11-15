# 每日收盘后更新行情数据
#
# 由 Windows 计划任务在工作日 16:05 调用（收盘 15:00 之后，东财日线已定稿）。
# 单独放一个脚本是因为：exe 打包后没有 scripts 目录，而计划任务需要能直接跑的
# 命令；同时日志要落到固定位置，出问题能查。
#
# 手动跑：
#   powershell -ExecutionPolicy Bypass -File scripts\daily_update.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\daily_update.ps1 -Bars 1400 -Limit 4800

param(
    [int]$Limit = 4800,
    [int]$Bars = 1400,
    [int]$Workers = 12,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
$log = Join-Path $logDir ("update_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}

# 打包后的 exe 里没有 python，用运行环境自带的；找不到就报错退出
if (-not $Python) {
    $candidates = @(
        "C:\Users\wang1\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
        (Join-Path $root ".venv\Scripts\python.exe"),
        "python"
    )
    foreach ($c in $candidates) {
        try {
            & $c --version 2>&1 | Out-Null
            $Python = $c
            break
        } catch { }
    }
}

if (-not $Python) {
    Write-Log "找不到 python，无法更新"
    exit 1
}

Write-Log "开始更新：limit=$Limit bars=$Bars workers=$Workers python=$Python"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $root
try {
'    # 主源：腾讯 ifzq（东财 push2his 批量拉会被限流返回 502）
    & $Python "scripts\fetch_history_tx.py" --limit $Limit --bars $Bars --workers $Workers 2>&1 |
        ForEach-Object { Write-Log $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "腾讯源失败（退出码 $LASTEXITCODE），回退东财"
        & $Python "scripts\fetch_history.py" --limit $Limit --bars $Bars --fqt 2 --workers 6 2>&1 |
            ForEach-Object { Write-Log $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-Log "东财也失败，退出码 $LASTEXITCODE"
            exit $LASTEXITCODE
        }
    }'
    Write-Log "完成"
} finally {
    Pop-Location
}
