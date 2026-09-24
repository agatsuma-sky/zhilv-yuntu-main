$ErrorActionPreference = "Stop"

$worktree = $env:WORKTREE_PATH
if ([string]::IsNullOrWhiteSpace($worktree)) {
    $worktree = $env:REPO_PATH
}
if ([string]::IsNullOrWhiteSpace($worktree)) {
    $worktree = (Get-Location).Path
}

$repo = $env:REPO_PATH
if ([string]::IsNullOrWhiteSpace($repo)) {
    $repo = $worktree
}

function Copy-Nested-Environment {
    param(
        [string] $RelativePath
    )

    $source = Join-Path $repo $RelativePath
    $target = Join-Path $worktree $RelativePath
    if ((Test-Path -LiteralPath $source -PathType Leaf) -and
        (-not (Test-Path -LiteralPath $target -PathType Leaf)) -and
        ($source -ne $target)) {
        $targetDirectory = Split-Path -LiteralPath $target -Parent
        New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target
    }
}

function Get-NpmPath {
    $command = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $nodeRoot = $env:ProgramFiles
    if ([string]::IsNullOrWhiteSpace($nodeRoot)) {
        $nodeRoot = ${env:ProgramW6432}
    }
    if ([string]::IsNullOrWhiteSpace($nodeRoot)) {
        throw "Node.js installation directory not found."
    }

    $npmPath = Join-Path $nodeRoot "nodejs\npm.cmd"
    if (-not (Test-Path -LiteralPath $npmPath -PathType Leaf)) {
        throw "npm.cmd not found. Install Node.js or add it to PATH."
    }
    return $npmPath
}

Copy-Nested-Environment "backend\.env"
Copy-Nested-Environment "frontend\.env"

$venvPath = Join-Path $worktree "travel"
$python = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pythonLauncher) {
        & py -3.11 -m venv $venvPath
    } else {
        & python -m venv $venvPath
    }
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $worktree "backend\requirements.txt")

$frontendPath = Join-Path $worktree "frontend"
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "node_modules") -PathType Container)) {
    $npmPath = Get-NpmPath
    & $npmPath --prefix $frontendPath ci --no-audit --no-fund
}
