# fix_ch340_epm.ps1 —— 需要以管理员身份运行(右键"使用 PowerShell 运行"选管理员)
# 作用: 给本机所有 CH340 串口设备禁用"增强电源管理"(EnhancedPowerManagementEnabled=0)
#
# 背景: 新版 ch341ser 驱动(3.8+)默认允许芯片空闲挂起省电,挂起后靠"远程唤醒"恢复。
#       板子挂在多级 hub 级联深处时,远程唤醒信号容易死在半路,表现为:
#       串口数据不丢但每 ~3 秒成批到达、命令往返 3 秒+。这是 CH340 的经典已知问题。
#
# 恢复方法(如需撤销): 把值改回 1,或删除该属性:
#   Remove-ItemProperty -Path <下面的key> -Name EnhancedPowerManagementEnabled

$ErrorActionPreference = 'Stop'

$devices = Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB\VID_1A86&PID_7523*' }
if (-not $devices) {
    Write-Output '没找到任何 CH340 设备(板子插好了吗?)'
    exit 1
}

foreach ($d in $devices) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($d.InstanceId)\Device Parameters"
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name 'EnhancedPowerManagementEnabled' -Value 0 -PropertyType DWord -Force | Out-Null
    $val = (Get-ItemProperty -Path $key).EnhancedPowerManagementEnabled
    Write-Output "$($d.InstanceId)  ->  EPM=$val"
}

Write-Output ''
Write-Output '完成。把板子重新插拔一次(或换到最终位置)让配置生效。'
