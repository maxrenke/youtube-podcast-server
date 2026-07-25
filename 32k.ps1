Add-Type -AssemblyName System.Windows.Forms

# Models to convert
$models = @(
    "qwen3.5:0.8b",
    "qwen3:8b",
    "llama3.2:1b",
    "deepseek-r1:1.5b"
)

Write-Host "Launching visible REPL sessions to create 32k variants..." -ForegroundColor Cyan

foreach ($model in $models) {

    $newModel = "$model-32k"
    Write-Host "`nProcessing $model → $newModel" -ForegroundColor Yellow

    # Launch Ollama REPL in a visible window
    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/k ollama run $model" `
        -WindowStyle Normal `
        -PassThru

    # Give it time to load the model
    Start-Sleep -Seconds 8

    # Activate the window
    $sig = '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);'
    $type = Add-Type -MemberDefinition $sig -Name "Win32SetForegroundWindow" -Namespace Win32Functions -PassThru
    $type::SetForegroundWindow($proc.MainWindowHandle)

    Start-Sleep -Milliseconds 500

    # Send REPL commands
    [System.Windows.Forms.SendKeys]::SendWait("/set parameter num_ctx 32768{ENTER}")
    Start-Sleep -Seconds 1

    [System.Windows.Forms.SendKeys]::SendWait("/save $newModel{ENTER}")
    Start-Sleep -Seconds 3

    [System.Windows.Forms.SendKeys]::SendWait("/bye{ENTER}")
    Start-Sleep -Seconds 1

    # Close the window
    $proc.CloseMainWindow() | Out-Null
    Start-Sleep -Seconds 1

    Write-Host "✔ Finished $newModel" -ForegroundColor Green
}

Write-Host "`nAll models processed." -ForegroundColor Cyan
