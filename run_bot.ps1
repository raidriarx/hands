param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$TaskParts,
    [string]$Speed = "Slow",
    [string]$Backend = "Antigravity + Ollama",
    [switch]$Ask,
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$entryScript = Join-Path $PSScriptRoot "main.py"

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python not found at $venvPython"
}

$cliArgs = @("--speed", $Speed, "--backend", $Backend)
if (-not $Ask) {
    $cliArgs += "--supervised"
}

$joinedTask = (($TaskParts -join " ") -replace "\s+", " ").Trim()
if ($joinedTask) {
    $cliArgs += @("--task", $joinedTask)
    if (-not $NoAutoStart) {
        $cliArgs += "--auto-start"
    }
}

& $venvPython $entryScript @cliArgs
exit $LASTEXITCODE
