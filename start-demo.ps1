$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Pnpm = "C:\Users\chen\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\pnpm.cmd"

$env:PYTHONPATH = $Root

Start-Process -FilePath $Python `
  -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8010") `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root "backend.out.log") `
  -RedirectStandardError (Join-Path $Root "backend.err.log")

Start-Process -FilePath $Pnpm `
  -ArgumentList @("dev", "--host", "127.0.0.1", "--port", "5174") `
  -WorkingDirectory (Join-Path $Root "frontend") `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root "frontend.out.log") `
  -RedirectStandardError (Join-Path $Root "frontend.err.log")

Write-Host "DCS Copilot Demo started:"
Write-Host "Backend:  http://127.0.0.1:8010"
Write-Host "Frontend: http://127.0.0.1:5174"
