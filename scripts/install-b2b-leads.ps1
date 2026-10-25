param([string]$Destination, [string]$Release)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskSource = Join-Path $taskRoot 'skills\b2b-leads'
if (-not (Test-Path -LiteralPath (Join-Path $taskSource 'SKILL.md'))) { throw 'Skill source is missing' }
if (-not $Destination) {
    $taskSkillsRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex\skills' }
    $Destination = Join-Path $taskSkillsRoot 'b2b-leads'
}
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = 'python' }
$taskArguments = @((Join-Path $taskRoot 'scripts\skill_release.py'), '--root', $taskRoot, '--destination', $Destination)
if ($Release) { $taskArguments += @('--release', $Release) }
& $taskPython @taskArguments
if ($LASTEXITCODE -ne 0) { throw 'Skill release installation failed; snapshots preserved' }
