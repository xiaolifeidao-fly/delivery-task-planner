param()

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptDirectory
$installRoot = Join-Path $HOME "plugins\delivery-task-planner"
$marketplaceFile = Join-Path $HOME ".agents\plugins\marketplace.json"

function Find-Or-CopyCodexCli {
  $command = Get-Command codex -ErrorAction SilentlyContinue
  if ($command) {
    return $command.Source
  }

  $localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME "AppData\Local" }
  $cacheDirectory = Join-Path $localAppData "delivery-task-planner\bin"
  $cachedCli = Join-Path $cacheDirectory "codex.exe"
  if (Test-Path -LiteralPath $cachedCli -PathType Leaf) {
    return $cachedCli
  }

  $programFiles = if ($env:ProgramFiles) { $env:ProgramFiles } else { "C:\Program Files" }
  $resourceCandidates = @(
    (Join-Path $localAppData "Programs\Codex\resources\codex.exe"),
    (Join-Path $localAppData "Programs\Codex Desktop\resources\codex.exe"),
    (Join-Path $localAppData "Codex\resources\codex.exe"),
    (Join-Path $localAppData "Codex Desktop\resources\codex.exe"),
    (Join-Path $programFiles "Codex\resources\codex.exe"),
    (Join-Path $programFiles "Codex Desktop\resources\codex.exe")
  )
  $sourceCli = $resourceCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
  if (-not $sourceCli) {
    return $null
  }

  New-Item -ItemType Directory -Path $cacheDirectory -Force | Out-Null
  Copy-Item -LiteralPath $sourceCli -Destination $cachedCli -Force
  $sourceHost = Join-Path (Split-Path -Parent $sourceCli) "codex-code-mode-host.exe"
  if (Test-Path -LiteralPath $sourceHost -PathType Leaf) {
    Copy-Item -LiteralPath $sourceHost -Destination (Join-Path $cacheDirectory "codex-code-mode-host.exe") -Force
  }
  return $cachedCli
}

$codexCommand = Find-Or-CopyCodexCli
if (-not $codexCommand) {
  throw "Codex CLI was not found. Install Codex Desktop so its resources\codex.exe can be copied, or install the standalone Codex CLI."
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
& $codexCommand plugin add delivery-task-planner@personal
if ($LASTEXITCODE -ne 0) {
  throw "Could not install the delivery-task-planner plugin."
}
& (Join-Path $installRoot "scripts\start_http.ps1")
