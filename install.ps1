param(
    [string]$PythonExe = "",
    [string]$BackupDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Resolve-PythonExe {
    param([string]$ExplicitPath)
    if ($ExplicitPath -and (Test-Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }
    $candidates = @()
    $localPyDir = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPyDir) {
        $candidates += Get-ChildItem -Path $localPyDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -ExpandProperty FullName
    }
    $cmdPy = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
    foreach ($c in $cmdPy) {
        if ($c.Source -and $c.Source -notmatch "WindowsApps") {
            $candidates += $c.Source
        }
    }
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            try {
                $verOut = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $verOut) {
                    $parts = $verOut.Trim().Split(".")
                    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                        return $cand
                    }
                }
            } catch {}
        }
    }
    throw "未找到可用的 Python 3.10+，请先安装 Python 或通过 -PythonExe 指定路径。"
}

function Resolve-AgyExe {
    $defaultPath = Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe"
    if (Test-Path $defaultPath) {
        return (Resolve-Path $defaultPath).Path
    }
    $cmd = Get-Command "agy.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    $cmd2 = Get-Command "agy" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd2 -and $cmd2.Source) {
        return $cmd2.Source
    }
    return ""
}

function Upsert-AgyProfileBlock {
    param(
        [string]$ProfilePath,
        [string]$BlockText
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $parentDir = Split-Path -Parent $ProfilePath
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }
    if (-not (Test-Path $ProfilePath)) {
        [System.IO.File]::WriteAllText($ProfilePath, $BlockText + [Environment]::NewLine, $utf8NoBom)
        return
    }

    $content = [System.IO.File]::ReadAllText($ProfilePath, [System.Text.Encoding]::UTF8)
    $startMarker = "# >>> agy-session-resume >>>"
    $endMarker = "# <<< agy-session-resume <<<"
    $markerPattern = "(?s)" + [regex]::Escape($startMarker) + ".*?" + [regex]::Escape($endMarker)
    if ([regex]::IsMatch($content, $markerPattern)) {
        $updated = [regex]::Replace($content, $markerPattern, $BlockText, 1)
        [System.IO.File]::WriteAllText($ProfilePath, $updated, $utf8NoBom)
        return
    }

    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($content, [ref]$tokens, [ref]$errors)
    $agyFunc = $ast.Find(
        {
            param($node)
            ($node -is [System.Management.Automation.Language.FunctionDefinitionAst]) -and ($node.Name -eq "agy")
        },
        $false
    )
    if ($null -ne $agyFunc) {
        $startOff = $agyFunc.Extent.StartOffset
        $endOff = $agyFunc.Extent.EndOffset
        $updated = $content.Substring(0, $startOff) + $BlockText + $content.Substring($endOff)
        [System.IO.File]::WriteAllText($ProfilePath, $updated, $utf8NoBom)
        return
    }

    $trimmed = $content.TrimEnd("`r", "`n")
    if ($trimmed.Length -gt 0) {
        $updated = $trimmed + [Environment]::NewLine + [Environment]::NewLine + $BlockText + [Environment]::NewLine
    } else {
        $updated = $BlockText + [Environment]::NewLine
    }
    [System.IO.File]::WriteAllText($ProfilePath, $updated, $utf8NoBom)
}

Write-Host "=== [1/5] 探测 Python 3.10+ 与 agy.exe 环境 ===" -ForegroundColor Cyan
$ResolvedPython = Resolve-PythonExe -ExplicitPath $PythonExe
$PyVer = & $ResolvedPython --version
Write-Host "  已定位 Python: $ResolvedPython ($PyVer)" -ForegroundColor Green

$ResolvedAgy = Resolve-AgyExe
if ($ResolvedAgy) {
    Write-Host "  已定位 agy: $ResolvedAgy" -ForegroundColor Green
} else {
    Write-Warning "  未在默认路径或 PATH 中定位到 agy.exe，请确认本机已安装 Google Antigravity CLI。"
}

Write-Host "=== [2/5] 解析安装目录并备份既有配置 ===" -ForegroundColor Cyan
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalBinDir = Join-Path $env:USERPROFILE ".local\bin"
$SourceScript = Join-Path $RepoRoot "agy_session_guard.py"
$DestScript = Join-Path $LocalBinDir "agy_session_guard.py"

if (-not $BackupDir) {
    if (Test-Path "D:\Project") {
        $BackupDir = "D:\Project\agy Backups"
    } elseif (Test-Path "D:\") {
        $BackupDir = "D:\Agy Backups"
    } else {
        $BackupDir = Join-Path $env:USERPROFILE "Agy Backups"
    }
}
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$DocsDir = [Environment]::GetFolderPath("MyDocuments")
$ProfileTargets = New-Object System.Collections.Generic.List[string]
$ProfileTargets.Add($PROFILE)
$PwshCmd = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$Ps7Profile = Join-Path $DocsDir "PowerShell\Microsoft.PowerShell_profile.ps1"
if ($PwshCmd -or (Test-Path $Ps7Profile)) {
    $ProfileTargets.Add($Ps7Profile)
}
$UniqueProfiles = @()
$SeenPaths = @{}
foreach ($pt in $ProfileTargets) {
    if ($pt) {
        $norm = $pt.ToLowerInvariant()
        if (-not $SeenPaths.ContainsKey($norm)) {
            $SeenPaths[$norm] = $true
            $UniqueProfiles += $pt
        }
    }
}

$NeedBackup = (Test-Path $DestScript)
if (-not $NeedBackup) {
    foreach ($pt in $UniqueProfiles) {
        if (Test-Path $pt) {
            $NeedBackup = $true
            break
        }
    }
}
if ($NeedBackup) {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $SnapDir = Join-Path $BackupDir "agy-session-resume-$ts"
    New-Item -ItemType Directory -Path $SnapDir -Force | Out-Null
    if (Test-Path $DestScript) {
        Copy-Item -Path $DestScript -Destination (Join-Path $SnapDir "agy_session_guard.py") -Force
    }
    $idx = 0
    foreach ($pt in $UniqueProfiles) {
        if (Test-Path $pt) {
            $idx++
            $tag = if ($pt -match "WindowsPowerShell") { "ps5" } elseif ($pt -match "PowerShell") { "ps7" } else { "ps$idx" }
            Copy-Item -Path $pt -Destination (Join-Path $SnapDir "Microsoft.PowerShell_profile.$tag.ps1") -Force
        }
    }
    Write-Host "  已备份既有文件至: $SnapDir" -ForegroundColor Green
} else {
    Write-Host "  默认备份根目录: $BackupDir（首次安装无需备份）" -ForegroundColor Green
}

Write-Host "=== [3/5] 部署运行副本并配置 PowerShell 入口 ===" -ForegroundColor Cyan
if (-not (Test-Path $LocalBinDir)) {
    New-Item -ItemType Directory -Path $LocalBinDir -Force | Out-Null
}
Copy-Item -Path $SourceScript -Destination $DestScript -Force
$SrcHash = (Get-FileHash -Path $SourceScript -Algorithm SHA256).Hash
$DstHash = (Get-FileHash -Path $DestScript -Algorithm SHA256).Hash
if ($SrcHash -ne $DstHash) {
    throw "运行副本哈希校验失败：$SrcHash != $DstHash"
}
Write-Host "  已同步运行副本: $DestScript (SHA256: $DstHash)" -ForegroundColor Green

$EscapedPy = $ResolvedPython.Replace("'", "''")
$ProfileBlock = @"
# >>> agy-session-resume >>>
function agy {
    `$agyExe = Join-Path `$env:LOCALAPPDATA 'agy\bin\agy.exe'
    if (-not (Test-Path `$agyExe)) {
        `$cmdAgy = Get-Command 'agy.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (`$cmdAgy) {
            `$agyExe = `$cmdAgy.Source
        } else {
            `$cmdAgy = Get-Command 'agy' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (`$cmdAgy) { `$agyExe = `$cmdAgy.Source }
        }
    }
    `$guardPy = Join-Path `$env:USERPROFILE '.local\bin\agy_session_guard.py'
    `$pythonExe = '$EscapedPy'
    if (-not (Test-Path `$pythonExe)) { `$pythonExe = 'python' }
    `$isPrintMode = `$false
    `$needsGuard = `$false
    foreach (`$a in `$args) {
        if (
            `$a -ceq '-p' -or
            `$a -ceq '--print' -or
            `$a -ceq '--prompt' -or
            `$a -clike '--prompt=*' -or
            `$a -ceq '-v' -or
            `$a -ceq '--version' -or
            `$a -ceq '-h' -or
            `$a -ceq '--help'
        ) {
            `$isPrintMode = `$true
            break
        }
        if (
            `$a -ceq '-r' -or
            `$a -ceq '--resume' -or
            `$a -ceq '-c' -or
            `$a -ceq '--continue' -or
            `$a -ceq '--conversation' -or
            `$a -clike '--conversation=*'
        ) {
            `$needsGuard = `$true
        }
    }
    if (-not `$isPrintMode -and `$needsGuard -and (Test-Path `$guardPy)) {
        & `$pythonExe -X utf8 `$guardPy @args
        return
    }
    if (-not (Test-Path `$agyExe)) {
        throw 'agy.exe not found. Please verify Google Antigravity CLI is installed.'
    }
    & `$agyExe @args
}
# <<< agy-session-resume <<<
"@

foreach ($pt in $UniqueProfiles) {
    Upsert-AgyProfileBlock -ProfilePath $pt -BlockText $ProfileBlock
    Write-Host "  已更新 PowerShell Profile: $pt" -ForegroundColor Green
}

Write-Host "=== [4/5] 运行离线沙盒单元测试 ===" -ForegroundColor Cyan
& $ResolvedPython -B -X utf8 -m unittest discover -s (Join-Path $RepoRoot "tests") -v
if ($LASTEXITCODE -ne 0) {
    throw "离线沙盒单元测试未通过，请检查输出日志。"
}

Write-Host "=== [5/5] 执行本机只读会话扫描自检 ===" -ForegroundColor Cyan
$ScanCode = "import sys; sys.path.insert(0, sys.argv[1]); import agy_session_guard as g; m = g._load_models_map(allow_refresh=False); s = g.load_sessions(m); print(f'  只读自检成功：当前设备共检测到 {len(s)} 条有效 Agy CLI 会话')"
& $ResolvedPython -B -X utf8 -c $ScanCode $LocalBinDir
if ($LASTEXITCODE -ne 0) {
    throw "本机只读会话扫描自检失败。"
}

Write-Host ""
Write-Host "✅ agy-session-resume 安装与自检全部通过！新开 PowerShell 窗口后即可使用 agy -r。" -ForegroundColor Green
