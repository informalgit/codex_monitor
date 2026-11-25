$targetScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'codex_monitor.pyw'))
$targetPattern = [regex]::Escape($targetScript)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match $targetPattern } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
