$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Ask Edgar V2 - Dilution Monitor.lnk")
$Shortcut.TargetPath = "$PSScriptRoot\dist\AskEdgarV2.exe"
$Shortcut.WorkingDirectory = "$PSScriptRoot\dist"
$Shortcut.IconLocation = "$PSScriptRoot\app_icon.ico,0"
$Shortcut.Description = "Ask Edgar Dilution Monitor V2"
$Shortcut.Save()
Write-Host "Desktop shortcut created: Ask Edgar V2 - Dilution Monitor"
