Get-CimInstance Win32_USBHub | Select-Object Name, DeviceID | Sort-Object DeviceID | Format-Table -AutoSize | Out-String -Width 200
