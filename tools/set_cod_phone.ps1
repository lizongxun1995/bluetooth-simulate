# set_cod_phone.ps1 —— 把本机蓝牙 COD(设备类) 改成 手机/智能手机
# 依据(官方驱动注册表条目, MS Learn "Bluetooth Registry Entries"):
#   HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters
#     COD Major (DWORD) = 2   # SIG Assigned Numbers: Major Device Class 2 = Phone
#     COD Type  (DWORD) = 3   # Minor Device Class 3 = Smart Phone
#   * 只覆盖 主/次设备类 位; 服务类位(Audio/Telephony...)由栈按已启用服务自动管理
#   * 未设/非法时默认 电脑/台式机 (COD_MAJOR_COMPUTER / COD_COMPUTER_MINOR_DESKTOP)
# 用法(需管理员 PowerShell):
#   powershell -ExecutionPolicy Bypass -File tools\set_cod_phone.ps1          # 改手机类
#   powershell -ExecutionPolicy Bypass -File tools\set_cod_phone.ps1 -Revert # 还原默认
# 生效与观察:
#   1. 脚本自动重启蓝牙射频(改 COD 在射频重新初始化时生效)
#   2. 对端会缓存配对时的 COD —— 必须"解除配对 → 重新配对"车机才会按手机类看待本机
#   3. 期望管理: 这只改"外衣", Windows 依旧没有 HFP AG 服务端/电话设备模型,
#      CallControl 不会因此复活; 实验目的 = 看车机是否改变设备图标/是否开始主动连通话音频
param([switch]$Revert)

$key = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "!! 需要管理员 PowerShell (右键开始菜单->终端(管理员))" -ForegroundColor Red; exit 1 }

if ($Revert) {
    Remove-ItemProperty -Path $key -Name 'COD Major' -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $key -Name 'COD Type' -ErrorAction SilentlyContinue
    Write-Host "已删除 COD Major / COD Type -> 恢复默认(电脑/台式机)" -ForegroundColor Yellow
} else {
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name 'COD Major' -Value 2 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name 'COD Type' -Value 3 -PropertyType DWord -Force | Out-Null
    Write-Host "已写入 COD Major=2 (Phone) / COD Type=3 (Smart Phone)" -ForegroundColor Green
}

# 重启蓝牙射频让栈重读 COD: 射频本体挂在 USB 总线下(Realtek/Intel 均如此)
$bt = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like 'USB*' -and $_.Status -eq 'OK' })
if ($bt.Count -eq 0) {
    Write-Host "!! 未自动找到蓝牙射频设备 —— 请手动重启蓝牙(设置->蓝牙开关, 或重启计算机)后生效" -ForegroundColor Red
} else {
    foreach ($d in $bt) {
        Write-Host "重启蓝牙射频: $($d.FriendlyName)  [$($d.InstanceId)]"
        Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false
        Start-Sleep -Seconds 2
        Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false
    }
}

Write-Host ""
Write-Host "下一步(必须): 等5s让栈起来 -> 解除配对车机 -> 全新配对(GUI 🔁重新配对) -> 观察车机图标/是否主动连通话音频" -ForegroundColor Cyan
Write-Host "还原: powershell -File tools\set_cod_phone.ps1 -Revert" -ForegroundColor DarkGray
