# 查询已配对蓝牙设备(按 MAC)在 Windows 侧装了哪些 profile 服务
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File tools\bt_profiles.ps1 [MAC默认382A8BC316B1]
param([string]$Mac = "382A8BC316B1")

"=== 所有 InstanceId 含 $Mac 的设备 (按类分组) ==="
Get-PnpDevice | Where-Object { $_.InstanceId -like "*$Mac*" } |
    Sort-Object Class | Format-List FriendlyName, Class, Status, InstanceId

"=== BTHENUM 服务 GUID 汇总 ==="
Get-PnpDevice | Where-Object { $_.InstanceId -like "BTHENUM*$Mac*" } |
    ForEach-Object { $_.InstanceId.Split('\')[1] + '   ->   ' + $_.FriendlyName + '  [' + $_.Status + ']' }
