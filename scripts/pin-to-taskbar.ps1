# Pin Story Lifecycle Server to Windows Taskbar
# Run this script once to create a taskbar-pinnable shortcut

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Story Server.lnk")
$Shortcut.TargetPath = "cmd.exe"
$Shortcut.Arguments = "/k `"`"D:\story-lifecycle\scripts\start-server.bat`"`""
$Shortcut.WorkingDirectory = "D:\story-lifecycle"
$Shortcut.Description = "Story Lifecycle Server"
$Shortcut.Save

Write-Host "Shortcut created on Desktop: Story Server.lnk"
Write-Host ""
Write-Host "To pin to taskbar:"
Write-Host "  1. Double-click the desktop shortcut to launch it once"
Write-Host "  2. Right-click the icon on the taskbar"
Write-Host "  3. Select 'Pin to taskbar'"
Write-Host ""
Write-Host "Or: Right-drag the shortcut to the taskbar"
