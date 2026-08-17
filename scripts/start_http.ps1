param(
  [string]$Workspace = ""
)

$ErrorActionPreference = "Stop"
$taskName = "Universe Delivery Task Planner Bridge"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginRoot = Split-Path -Parent $scriptDirectory
$runtimeDirectory = if ($env:LOCALAPPDATA) {
  Join-Path $env:LOCALAPPDATA "delivery-task-planner"
} else {
  Join-Path $HOME ".local\state\delivery-task-planner"
}
$workspaceFile = Join-Path $runtimeDirectory "workspace"

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
if (-not $Workspace -and (Test-Path -LiteralPath $workspaceFile)) {
  $Workspace = (Get-Content -LiteralPath $workspaceFile -Raw).Trim()
}
if ($Workspace) {
  [System.IO.File]::WriteAllText($workspaceFile, "$Workspace`n", [System.Text.UTF8Encoding]::new($false))
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
  $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
  $pythonArguments = @("-3")
}
if (-not $pythonCommand) {
  throw "Python 3 is required to run the delivery HTTPS bridge."
}

& $pythonCommand.Source @pythonArguments (Join-Path $scriptDirectory "ensure_https_cert.py") --install-trust
if ($LASTEXITCODE -ne 0) {
  throw "Could not initialize the local HTTPS certificate."
}

& (Join-Path $scriptDirectory "install_http_service.ps1") -PluginRoot $pluginRoot -Workspace $Workspace -AllowOrigin "*"
for ($attempt = 0; $attempt -lt 30; $attempt += 1) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "https://127.0.0.1:8765/healthz" -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
      Write-Output "Codex HTTPS bridge is running at https://127.0.0.1:8765"
      exit 0
    }
  } catch {
    Start-Sleep -Milliseconds 200
  }
}

throw "Codex HTTPS bridge failed to start. Check the scheduled task '$taskName'."
