$pkg = Get-AppxPackage -Name 'Microsoft.VisualStudioCode'
if ($pkg) {
    Write-Output "Found: $($pkg.PackageFamilyName)"
    $manifest = Get-AppxPackageManifest -Package $pkg
    $apps = $manifest.Package.Applications.Application
    foreach ($a in $apps) {
        Write-Output "  $($a.Id) -> $($a.Executable)"
    }
} else {
    Write-Output "NOT FOUND by Name"
    $all = Get-AppxPackage | Where-Object { $_.Name -eq 'Microsoft.VisualStudioCode' -or $_.PackageFamilyName -like 'Microsoft.VisualStudioCode*' }
    Write-Output "By filter: $($all.Count) matches"
    foreach ($a in $all) {
        Write-Output "  Name: $($a.Name) PFN: $($a.PackageFamilyName)"
    }
}
