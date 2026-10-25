param([switch]$Database)
$ErrorActionPreference = 'Stop'
# Acquisition runs in each command process; no API or worker is started.
if ($Database) {
    $taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    Push-Location -LiteralPath $taskRoot
    try { & '.\.venv\Scripts\python.exe' scripts/local_database.py stop } finally { Pop-Location }
} else { Write-Output 'No persistent acquisition service to stop. Pause a run with status --action pause.' }
