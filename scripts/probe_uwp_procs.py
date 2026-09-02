"""Probe UWP packages and their process names."""
import subprocess
import json

# PowerShell script to get package info
script = r'''
$pkgs = Get-AppxPackage | Where-Object { $_.Name -match 'Calculator|Terminal|Notepad|Spotify|Code|MicrosoftEdge|Microsoft.MSPaint|Discord|WhatsApp' } | Select-Object Name, InstallLocation, PackageFamilyName | ConvertTo-Json
Write-Output $pkgs
'''
proc = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True, timeout=30)
print("STDOUT:", proc.stdout[:2000])
print("STDERR:", proc.stderr[:500])
