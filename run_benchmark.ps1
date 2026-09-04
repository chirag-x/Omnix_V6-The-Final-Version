$commands = @(
    "Open Notepad",
    "Close Notepad",
    "Open Calculator"
)

foreach ($cmd in $commands) {
    Write-Host "Running: $cmd"
    .\.venv\Scripts\python main.py process "$cmd"
    Start-Sleep -Seconds 2
}
