$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $taskRoot
try {
    & $taskPython scripts/b2b_leads.py prepare
    if ($LASTEXITCODE -ne 0) { throw 'Skill preparation failed' }
} finally { Pop-Location }
