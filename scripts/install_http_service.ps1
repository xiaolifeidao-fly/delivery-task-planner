param(
  [Parameter(Mandatory = $true)]
  [string]$PluginRoot,
  [string]$Workspace = "",
  [string]$CommandApiUrl = "",
  [string]$AllowOrigin = "*"
)

$ErrorActionPreference = "Stop"
$taskName = "Universe Delivery Task Planner Bridge"
$bridgeScript = Join-Path $PluginRoot "http_bridge.py"
$supervisorScript = Join-Path $PluginRoot "delivery_bridge\windows_supervisor.py"
if (-not (Test-Path -LiteralPath $bridgeScript)) {
  throw "Bridge script not found: $bridgeScript"
}
if (-not (Test-Path -LiteralPath $supervisorScript)) {
  throw "Windows supervisor script not found: $supervisorScript"
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
  $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
  $pythonArguments = @("-3")
}
if (-not $pythonCommand) {
  throw "Python 3 is required to run the delivery HTTP bridge."
}

$arguments = @()
$arguments += $pythonArguments
$arguments += '"' + $supervisorScript + '"'
$arguments += "--plugin-root"
$arguments += '"' + $PluginRoot + '"'
if ($Workspace) {
  $arguments += "--workspace"
  $arguments += '"' + $Workspace + '"'
}
if ($CommandApiUrl) {
  $arguments += "--command-api-url"
  $arguments += '"' + $CommandApiUrl + '"'
}
$arguments += "--allow-origin"
$arguments += '"' + $AllowOrigin + '"'

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType InteractiveToken -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $pythonCommand.Source -Argument ($arguments -join " ")
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
  -RestartCount 10 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -StartWhenAvailable
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Local HTTP bridge for the Universe delivery task board." -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
