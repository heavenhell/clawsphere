$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
if ($PnpmCommand) {
  $PackageManager = $PnpmCommand.Source
  $FrontendArguments = @("dev", "--host", "127.0.0.1", "--port", "5174")
} else {
  $NpmCommand = Get-Command npm.cmd -ErrorAction Stop
  $PackageManager = $NpmCommand.Source
  $FrontendArguments = @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5174")
}

$env:PYTHONPATH = $Root

Start-Process -FilePath $Python `
  -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8010") `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root "backend.out.log") `
  -RedirectStandardError (Join-Path $Root "backend.err.log")

Start-Process -FilePath $PackageManager `
  -ArgumentList $FrontendArguments `
  -WorkingDirectory (Join-Path $Root "frontend") `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root "frontend.out.log") `
  -RedirectStandardError (Join-Path $Root "frontend.err.log")

$PlatformConfigPath = Join-Path $Root "config\platforms.json"
$PlatformConfig = $null
if (Test-Path -LiteralPath $PlatformConfigPath) {
  $PlatformConfig = Get-Content -LiteralPath $PlatformConfigPath -Raw | ConvertFrom-Json
  if ($PlatformConfig.mcp.enabled) {
    Start-Process -FilePath $Python `
      -ArgumentList @("-m", "backend.mcp.mcp_server") `
      -WorkingDirectory $Root `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $Root "mcp.out.log") `
      -RedirectStandardError (Join-Path $Root "mcp.err.log")
  }
}

Write-Host "DCS Copilot Demo started:"
Write-Host "Backend:  http://127.0.0.1:8010"
Write-Host "Frontend: http://127.0.0.1:5174"
if ($PlatformConfig -and $PlatformConfig.mcp.enabled) {
  Write-Host "MCP Server: http://$($PlatformConfig.mcp.host):$($PlatformConfig.mcp.port)/mcp"
}
if ($PlatformConfig -and $PlatformConfig.mcp.agent_mode -eq "mcp") {
  $McpEndpoint = if ($PlatformConfig.mcp.url) {
    $PlatformConfig.mcp.url
  } else {
    "http://$($PlatformConfig.mcp.host):$($PlatformConfig.mcp.port)/mcp"
  }
  Write-Host "Agent MCP:  $McpEndpoint"
}
