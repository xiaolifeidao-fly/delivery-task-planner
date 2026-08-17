param()

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptDirectory
$installRoot = Join-Path $HOME "plugins\delivery-task-planner"
$marketplaceFile = Join-Path $HOME ".agents\plugins\marketplace.json"

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
  throw "The Codex CLI is required."
}
if (-not (Test-Path -LiteralPath $marketplaceFile)) {
  throw "The personal marketplace entry for delivery-task-planner is missing."
}
if (-not (Get-Content -LiteralPath $marketplaceFile -Raw | Select-String -Quiet '"name"\s*:\s*"delivery-task-planner"')) {
  throw "The personal marketplace entry for delivery-task-planner is missing."
}

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
& robocopy $pluginRoot $installRoot /MIR /XD __pycache__ /XF *.pyc | Out-Null
if ($LASTEXITCODE -gt 7) {
  throw "Could not copy the plugin to $installRoot."
}
& codex plugin add delivery-task-planner@personal
if ($LASTEXITCODE -ne 0) {
  throw "Could not install the delivery-task-planner plugin."
}
& (Join-Path $installRoot "scripts\start_http.ps1")
