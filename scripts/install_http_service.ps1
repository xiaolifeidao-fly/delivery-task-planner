param(
  [Parameter(Mandatory = $true)]
  [string]$PluginRoot,
  [string]$Workspace = "",
  [string]$AllowOrigin = "*"
)

$ErrorActionPreference = "Stop"
$taskName = "Universe Delivery Task Planner Bridge"
$bridgeScript = Join-Path $PluginRoot "http_bridge.py"
if (-not (Test-Path -LiteralPath $bridgeScript)) {
  throw "Bridge script not found: $bridgeScript"
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
$arguments += '"' + $bridgeScript + '"'
if ($Workspace) {
  $arguments += "--workspace"
  $arguments += '"' + $Workspace + '"'
}
$arguments += "--allow-origin"
$arguments += '"' + $AllowOrigin + '"'

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType InteractiveToken -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $pythonCommand.Source -Argument ($arguments -join " ")
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Local HTTP bridge for the Universe delivery task board." -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
