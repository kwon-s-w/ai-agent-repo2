param([int]$Port = 8765)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')) {
    $pythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
} elseif (Test-Path -LiteralPath $bundledPython) {
    $pythonExecutable = $bundledPython
} elseif ($pythonCommand) {
    $pythonExecutable = $pythonCommand.Source
} else {
    throw 'Python 3.11 이상을 설치하고 README의 설치 절차를 실행하세요.'
}
& $pythonExecutable -X utf8 (Join-Path $PSScriptRoot 'server.py') --port $Port
