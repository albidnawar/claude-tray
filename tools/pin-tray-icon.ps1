<#
.SYNOPSIS
    Moves the Claude Pulse tray icon out of the "^" overflow flyout, next to Wi-Fi and volume.

.DESCRIPTION
    Windows 11 hides every new tray icon until it is "promoted". This sets the same per-user flag
    that Settings > Personalization > Taskbar > Other system tray icons sets, for the Python
    interpreter that runs Claude Pulse. Run it after the app has been started once.
    Use -Undo to put the icon back into the overflow flyout.
#>
param([switch]$Undo)

$root = 'HKCU:\Control Panel\NotifyIconSettings'
$value = if ($Undo) { 0 } else { 1 }
$changed = 0

Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
    $entry = Get-ItemProperty $_.PSPath
    if ($entry.ExecutablePath -match '\\pythonw?\.exe$') {
        Set-ItemProperty -Path $_.PSPath -Name IsPromoted -Value $value -Type DWord
        Write-Output "IsPromoted=$value  $($entry.ExecutablePath)"
        $changed++
    }
}

if ($changed -eq 0) {
    Write-Output 'No Python tray entry found. Start Claude Pulse once, then run this script again.'
}
