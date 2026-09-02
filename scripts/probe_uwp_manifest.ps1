$names = @('Microsoft.WindowsCalculator','Microsoft.WindowsTerminal','Microsoft.WindowsNotepad','Microsoft.VisualStudioCode','SpotifyAB.SpotifyMusic','Microsoft.ScreenSketch','Microsoft.MicrosoftEdge.Stable')
foreach ($n in $names) {
    $pkg = Get-AppxPackage -Name $n
    if ($pkg) {
        $apps = (Get-AppxPackageManifest -Package $pkg).Package.Applications.Application
        foreach ($a in $apps) {
            Write-Output ("{0}|{1}|{2}" -f $n, $a.Id, $a.Executable)
        }
    } else {
        Write-Output ("{0}|NOT_FOUND|" -f $n)
    }
}
