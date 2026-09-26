$ErrorActionPreference = "Stop"

$worktree = $env:WORKTREE_PATH
if ([string]::IsNullOrWhiteSpace($worktree)) {
    $worktree = $env:REPO_PATH
}
if ([string]::IsNullOrWhiteSpace($worktree)) {
    $worktree = (Get-Location).Path
}

$backendPath = Join-Path $worktree "backend"
$frontendPath = Join-Path $worktree "frontend"
$python = Join-Path $worktree "travel\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python virtual environment not found at $python. Run .kilo/setup-script.ps1 first."
}
if (-not (Test-Path -LiteralPath (Join-Path $backendPath "app\api\main.py") -PathType Leaf)) {
    throw "Backend application not found at $backendPath."
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "package.json") -PathType Leaf)) {
    throw "Frontend package not found at $frontendPath."
}

# 确保 Node.js 在 PATH 中（适配 winget/choco 安装或手动添加的 Node.js）
$nodeJsDirs = @()
$cmd = Get-Command node -ErrorAction SilentlyContinue
if ($null -ne $cmd) {
    $nodeJsDirs += Split-Path -Parent $cmd.Source
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -ne $npmCommand) {
    $npmPath = $npmCommand.Source
} else {
    $npmPath = Join-Path $env:ProgramFiles "nodejs\npm.cmd"
    $nodeJsDirs += Join-Path $env:ProgramFiles "nodejs"
}
if (-not (Test-Path -LiteralPath $npmPath -PathType Leaf)) {
    throw "npm.cmd not found. Install Node.js or add it to PATH."
}
# 将 Node.js 目录添加到当前进程的 PATH，确保子进程能找到 node/vite 等可执行文件
$nodeJsDir = ($nodeJsDirs | Where-Object { Test-Path $_ -PathType Container } | Select-Object -First 1)
if ($nodeJsDir -and -not ($env:PATH -split ';' | Where-Object { $_ -eq $nodeJsDir })) {
    $env:PATH = "$nodeJsDir;$env:PATH"
}

# 确保前端依赖已安装
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "node_modules") -PathType Container)) {
    Write-Host "前端依赖未安装，正在安装..." -ForegroundColor Yellow
    & $npmPath --prefix $frontendPath ci --no-audit --no-fund
}

$pathBytes = [System.Text.Encoding]::UTF8.GetBytes($worktree)
$hashBytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash($pathBytes)
$backendOffset = ([int] $hashBytes[0] * 256) + [int] $hashBytes[1]
$frontendOffset = ([int] $hashBytes[2] * 256) + [int] $hashBytes[3]
$backendPort = 18000 + ($backendOffset % 1000)
$frontendPort = 15173 + ($frontendOffset % 1000)

$env:VITE_API_BASE_URL = "http://127.0.0.1:$backendPort"

$backendArguments = @(
    "-m", "uvicorn", "app.api.main:app",
    "--app-dir", $backendPath,
    "--host", "127.0.0.1",
    "--port", $backendPort.ToString()
)
$frontendArguments = @(
    "run", "dev", "--",
    "--host", "127.0.0.1",
    "--port", $frontendPort.ToString()
)

$backendProcess = Start-Process -FilePath $python -ArgumentList $backendArguments -WorkingDirectory $backendPath -NoNewWindow -PassThru
$frontendProcess = Start-Process -FilePath $npmPath -ArgumentList $frontendArguments -WorkingDirectory $frontendPath -NoNewWindow -PassThru

Write-Host "智旅云图已启动" -ForegroundColor Green
Write-Host "  Frontend: http://127.0.0.1:$frontendPort"
Write-Host "  Backend:  http://127.0.0.1:$backendPort"
Write-Host ('  API docs: http://127.0.0.1:{0}/docs' -f $backendPort)

try {
    while (-not $backendProcess.HasExited -and -not $frontendProcess.HasExited) {
        Start-Sleep -Seconds 1
    }
}
finally {
    foreach ($process in @($backendProcess, $frontendProcess)) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $backendProcess.HasExited -or $backendProcess.ExitCode -ne 0) {
    exit 1
}
if (-not $frontendProcess.HasExited -or $frontendProcess.ExitCode -ne 0) {
    exit 1
}
