$ErrorActionPreference = 'Stop'

$projectPath = $PSScriptRoot
$batchPath = Join-Path $projectPath '运行水源增量日报.bat'
$pythonPath = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$taskName = 'Shuiyuan-Incremental-Noon'
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$cmdPath = Join-Path $env:SystemRoot 'System32\cmd.exe'

if (-not (Test-Path -LiteralPath $batchPath -PathType Leaf)) {
    throw "找不到增量入口：$batchPath"
}
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw '请先将 Windows 时区设为北京时间，再安装每天 12:00 的任务。'
}

$arguments = '/d /c ""{0}" --scheduled "{1}""' -f $batchPath, $pythonPath
$action = New-ScheduledTaskAction -Execute $cmdPath -Argument $arguments -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Daily -At '12:00'
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description '每天北京时间 12:00 运行水源增量日报；需 Windows 用户已登录；错过时补跑。' -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output "已设置任务：$taskName"
Write-Output "每天北京时间 12:00 运行：$batchPath"
Write-Output "下次运行：$($info.NextRunTime)"
Write-Output "运行日志：$(Join-Path $projectPath 'outputs\scheduled_run.log')"
