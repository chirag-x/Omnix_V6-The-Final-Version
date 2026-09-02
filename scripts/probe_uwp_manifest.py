"""Test parsing the AppxManifest.xml directly."""
import subprocess
import re

# Get the manifest XML and parse it
script = r'''
$pkg = Get-AppxPackage -Name "Microsoft.WindowsCalculator"
$manifest = Get-AppxPackageManifest -Package $pkg
$manifest.Path
'''
proc = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True, timeout=30)
print("STDOUT:")
print(proc.stdout)
print("STDERR:", proc.stderr[:500])
