param(
    [switch]$SkipVerify,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-PythonCandidate {
    param(
        [string]$Exe,
        [string[]]$BaseArgs
    )
    try {
        $ver = & $Exe @BaseArgs -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}'); sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ver) {
            return [pscustomobject]@{
                Exe = $Exe
                BaseArgs = $BaseArgs
                Version = ($ver | Select-Object -First 1).ToString().Trim()
            }
        }
    }
    catch {
        return $null
    }
    return $null
}

$resolvedPy = $null
if ($Python -and $Python.Trim()) {
    $resolvedPy = Test-PythonCandidate -Exe $Python.Trim() -BaseArgs @()
    if (-not $resolvedPy) {
        throw "Provided Python executable '$Python' was not found or is older than Python 3.10."
    }
}
else {
    $candidates = @(
        @{ Exe = "py"; BaseArgs = @("-3.12") },
        @{ Exe = "py"; BaseArgs = @("-3") },
        @{ Exe = "python"; BaseArgs = @() }
    )
    foreach ($cand in $candidates) {
        if (Get-Command $cand.Exe -ErrorAction SilentlyContinue) {
            $resolvedPy = Test-PythonCandidate -Exe $cand.Exe -BaseArgs $cand.BaseArgs
            if ($resolvedPy) {
                break
            }
        }
    }
}

if (-not $resolvedPy) {
    throw "Python 3.10+ not found. Install Python 3.12+ or pass -Python <path_to_python.exe>."
}

Write-Host "[setup] Using Python $($resolvedPy.Version) via $($resolvedPy.Exe) $($resolvedPy.BaseArgs -join ' ')"

$venvDir = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[setup] Creating virtual environment at $venvDir..."
    & $resolvedPy.Exe @($resolvedPy.BaseArgs) -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Failed to create virtual environment at $venvDir."
    }
}
else {
    Write-Host "[setup] Reusing existing virtual environment at $venvDir"
}

$reqFile = Join-Path $PSScriptRoot "requirements.txt"
Write-Host "[setup] Installing dependencies from requirements.txt..."
& $venvPython -m pip install -r $reqFile
if ($LASTEXITCODE -ne 0) {
    throw "pip install -r requirements.txt failed with exit code $LASTEXITCODE."
}

$workspaceDir = Join-Path $PSScriptRoot "approved_workspace"
if (-not (Test-Path $workspaceDir)) {
    New-Item -ItemType Directory -Force -Path $workspaceDir | Out-Null
}
Write-Host "[setup] Sandbox workspace ready: $workspaceDir"

if (-not $SkipVerify) {
    Write-Host "[setup] Running pytest test suite..."
    & $venvPython -m pytest (Join-Path $PSScriptRoot "tests") -q
    if ($LASTEXITCODE -ne 0) {
        throw "Pytest failed with exit code $LASTEXITCODE."
    }

    Write-Host "[setup] Running headless UI smoke check..."
    & $venvPython (Join-Path $PSScriptRoot "main.py") --headless-check --speed Slow --backend "Antigravity + Ollama" --supervised
    if ($LASTEXITCODE -ne 0) {
        throw "Headless smoke check failed with exit code $LASTEXITCODE."
    }
}

Write-Host ""
Write-Host "[setup] Checking optional local runtimes..."
try {
    $ollamaStatus = & $venvPython -c "import httpx; r = httpx.get('http://127.0.0.1:11434/api/tags', timeout=2.0); models = [m.get('name','') for m in r.json().get('models',[])]; has_qwen = any(m.startswith('qwen3.5') for m in models); print('OK|' + ','.join(models[:6]) + '|' + ('YES' if has_qwen else 'NO'))" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ollamaStatus -and $ollamaStatus.StartsWith("OK|")) {
        $parts = $ollamaStatus.Split("|")
        $hasQwen = $parts[2]
        Write-Host "[setup] Ollama: reachable at http://127.0.0.1:11434 (models: $($parts[1]))"
        if ($hasQwen -ne "YES") {
            Write-Host "[setup] Tip: pull the default vision model with: ollama pull qwen3.5:latest"
        }
    }
    else {
        Write-Host "[setup] Ollama: not reachable at http://127.0.0.1:11434 (start 'ollama serve' and run 'ollama pull qwen3.5:latest' for vision execution)"
    }
}
catch {
    Write-Host "[setup] Ollama: not reachable at http://127.0.0.1:11434 (start 'ollama serve' and run 'ollama pull qwen3.5:latest')"
}

$agyPath = $null
$agyCmd = Get-Command agy -ErrorAction SilentlyContinue
if ($agyCmd) {
    $agyPath = $agyCmd.Source
}
elseif ($env:LOCALAPPDATA) {
    $candidateAgy = Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe"
    if (Test-Path $candidateAgy) {
        $agyPath = $candidateAgy
    }
}

if ($agyPath) {
    Write-Host "[setup] Antigravity CLI: found at $agyPath"
}
else {
    Write-Host "[setup] Antigravity CLI: not found (optional; OllamaPlanInterpreter fallback or Local/Cloud backends remain available)"
}

Write-Host ""
Write-Host "[setup] Done! Quick launch commands:"
Write-Host "  .\run_bot.ps1"
Write-Host "  .\run_bot.ps1 open instagram and text My Goldfish hi"
