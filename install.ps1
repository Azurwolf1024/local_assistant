# 本地语音助手 · 一键安装（Windows）
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -DryRun          # 只打印要做什么
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Dir D:\local_AI
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Zip .\local_assistant-1.1.0-source.zip
#
# 它只做三件事：① 拿到代码（git clone / 解压 zip）② 建 .venv 并装依赖
# ③ 把剩下的交给 `python main.py setup`（下模型 + 体检 + 告诉你下一步）。
#
# ★为什么壳里只有这么点逻辑★：Windows 上的 .ps1/.cmd 在中文、引号、编码上全是坑
# （这个仓库踩过五次以上），所以判断都写在能被自测钉住的 Python 里（voice_loop/setup_flow.py），
# 这里只负责「建 venv + pip install + 回调」这三件事 —— 它们用 Python 做反而更麻烦。
#
# ★本文件必须带 UTF-8 BOM★：PowerShell 5.1 会按 GBK 读没有 BOM 的 .ps1，中文会解析崩。
# 改的时候别把 BOM 弄丢了（编辑器里看起来只是一个看不见的字符）。

[CmdletBinding()]
param(
    [string]$Dir = ".",
    [string]$Repo = "https://github.com/Azurwolf1024/local_assistant.git",
    [string]$Branch = "main",
    [string]$Zip = "",
    [string]$Python = "",
    [string]$Mirror = "",
    [switch]$NoModels,
    [switch]$SkipDeps,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Say($msg, $kind = "info") {
    $mark = @{ info = "  ·"; ok = "  +"; warn = "  !"; err = "  x" }[$kind]
    Write-Host "$mark $msg"
}

function Head($text) {
    Write-Host ""
    Write-Host "== $text " -NoNewline
    Write-Host ("=" * [Math]::Max(0, 60 - $text.Length))
}

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Invoke-Step($title, [string[]]$Cmd) {
    Say "$title" "info"
    Say ("    " + ($Cmd -join " ")) "info"
    if ($DryRun) { return }
    if ($Cmd.Length -gt 1) {
        & $Cmd[0] $Cmd[1..($Cmd.Length - 1)]
    } else {
        & $Cmd[0]
    }
    if ($LASTEXITCODE -ne 0) { throw "$title 失败（退出码 $LASTEXITCODE）" }
}

Head "本地语音助手 · 一键安装"
if ($DryRun) { Say "这是试运行（-DryRun）：只打印要做什么，不真的动你的机器" "warn" }

# ---------------------------------------------------------------- 1) 拿到代码
$root = $null
if ($Zip -ne "") {
    if (-not (Test-Path $Zip)) { throw "找不到压缩包：$Zip" }
    $root = (Resolve-Path $Dir).Path
    Say "解压 $Zip → $root" "info"
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path $root | Out-Null
        Expand-Archive -Path $Zip -DestinationPath $root -Force
        # 压缩包里套了一层 local_assistant-<版本>/，把它里面的东西提到根目录
        $inner = Get-ChildItem $root -Directory | Where-Object { $_.Name -like "local_assistant-*" } |
            Select-Object -First 1
        if ($inner -and -not (Test-Path (Join-Path $root "main.py"))) {
            Say "把 $($inner.Name)\ 里的内容提到 $root" "info"
            Get-ChildItem $inner.FullName -Force | Move-Item -Destination $root -Force
            Remove-Item $inner.FullName -Recurse -Force
        }
    }
} else {
    if (-not (Test-Path $Dir)) {
        if ($DryRun) { Say "会新建目录 $Dir 并 git clone" "info" }
        else { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
    }
    $root = (Resolve-Path $Dir).Path
    if (Test-Path (Join-Path $root "main.py")) {
        Say "$root 里已经有代码（跳过 clone）" "ok"
        if ((Test-Path (Join-Path $root ".git")) -and (Have "git") -and -not $DryRun) {
            Say "顺便 git fetch，看有没有新版本…" "info"
            try { & git -C $root fetch --prune origin 2>$null | Out-Null } catch { }
        }
    } else {
        if (-not (Have "git")) {
            throw "这里没有 git，也没有可用的代码：请用 -Zip <下载的压缩包> 安装，或先装 git"
        }
        Invoke-Step "git clone（分支 $Branch）" @("git", "clone", "--branch", $Branch, $Repo, $root)
    }
}

if (-not (Test-Path (Join-Path $root "main.py"))) { throw "$root 里没有 main.py，代码没到位" }

# 路径带空格/中文时，原生库偶尔会抽风 —— 提醒一句（不拦，因为很多人就这样跑得好好的）
if ($root -match " " -or $root -notmatch "^[\x20-\x7E]+$") {
    Say "项目路径里有空格或中文：$root" "warn"
    Say "    原生库（音频/推理）偶尔会在这类路径上加载失败；换成 D:\local_AI 这种最省事" "warn"
}

# ---------------------------------------------------------------- 2) Python
Head "Python 与虚拟环境"
$py = $null
if ($Python -ne "") { $py = $Python }
elseif (Have "py") { $py = "py" ; $pyArgs = @("-3") }
elseif (Have "python") { $py = "python"; $pyArgs = @() }
else { throw "找不到 Python。装一个 3.11 以上版本（勾选 Add to PATH）再跑这个脚本" }
if (-not $pyArgs) { $pyArgs = @() }

$venv = Join-Path $root ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"

if (Test-Path $venvPy) {
    Say "已经有虚拟环境：$venv" "ok"
} else {
    Invoke-Step "建虚拟环境（$py $($pyArgs -join ' ') -m venv .venv）" (@($py) + $pyArgs + @("-m", "venv", $venv))
}

if (-not $DryRun) {
    $ver = & $venvPy -c "import sys;print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境里的 python 跑不起来：$venvPy" }
    Say "虚拟环境 Python $ver → $venvPy" "ok"
    $parts = $ver.Split(".")
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
        throw "需要 Python 3.11 以上（现在 $ver）。删掉 $venv 后用新版 Python 重建"
    }
}

# ---------------------------------------------------------------- 3) 依赖
Head "安装依赖"
if ($SkipDeps) {
    Say "按要求跳过依赖安装（-SkipDeps）" "warn"
} else {
    Invoke-Step "升级 pip" @($venvPy, "-m", "pip", "install", "--upgrade", "pip")
    $pipArgs = @($venvPy, "-m", "pip", "install", "-r", (Join-Path $root "requirements.txt"))
    if ($Mirror -ne "") { $pipArgs += @("-i", $Mirror) }
    Invoke-Step "装 requirements.txt（第一次要几分钟）" $pipArgs
}

# ---------------------------------------------------------------- 4) 剩下的交给 Python
Head "下模型 + 体检（交给 main.py setup）"
$setupArgs = @($venvPy, (Join-Path $root "main.py"), "setup", "--skip-deps")
if ($NoModels) { $setupArgs += "--skip-models" }
Invoke-Step "python main.py setup" $setupArgs

Head "完成"
Say "启动语音服务（前台试一次）：  $venvPy main.py listen" "ok"
Say "打开网页控制台：            $venvPy main.py ui" "ok"
Say "以后升级：                  $venvPy main.py upgrade --apply" "ok"
Say "搬家到别的机器：            $venvPy main.py doctor（只读体检）" "info"
Say "详细说明见 README 第 3 节（安装）与第 4 节（使用）" "info"
