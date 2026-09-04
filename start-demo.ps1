param(
  [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

$env:PYTHONPATH = $Root

$AgentPlatformConfigPath = if ($env:DCS_AGENT_PLATFORM_CONFIG) {
  $env:DCS_AGENT_PLATFORM_CONFIG
} elseif ($env:DCS_PLATFORM_CONFIG) {
  $env:DCS_PLATFORM_CONFIG
} else {
  Join-Path $Root "config\platforms.json"
}
$PlatformConfig = $null
if (Test-Path -LiteralPath $AgentPlatformConfigPath) {
  $PlatformConfig = Get-Content -LiteralPath $AgentPlatformConfigPath -Raw | ConvertFrom-Json
}
$env:DCS_AGENT_PLATFORM_CONFIG = $AgentPlatformConfigPath

$McpServerConfigAvailable = $true
$DefaultServerConfigPath = Join-Path $Root "config\platforms.mcp-server.json"
$AgentEdme = if ($PlatformConfig) { $PlatformConfig.edme } else { $null }
$AgentEdmeAuthMode = if ($AgentEdme -and $AgentEdme.auth_mode) {
  ([string]$AgentEdme.auth_mode).Trim().ToLowerInvariant()
} else {
  "server"
}
$AgentEdmeEndpoint = if ($AgentEdme -and $AgentEdme.ip) {
  ([string]$AgentEdme.ip).Trim()
} else {
  ""
}
$ClientDelegatedEdme = $AgentEdmeAuthMode -eq "client" -and $AgentEdmeEndpoint
if ($env:DCS_MCP_SERVER_PLATFORM_CONFIG) {
  $ServerPlatformConfigPath = $env:DCS_MCP_SERVER_PLATFORM_CONFIG
} elseif ($ClientDelegatedEdme) {
  $ServerPlatformConfigPath = $DefaultServerConfigPath
} else {
  $ServerPlatformConfigPath = $AgentPlatformConfigPath
}
if ($ClientDelegatedEdme -and -not (Test-Path -LiteralPath $ServerPlatformConfigPath)) {
  $McpServerConfigAvailable = $false
  Write-Warning "MCP Server config is missing; eDME client delegation will remain unavailable."
} else {
  $env:DCS_MCP_SERVER_PLATFORM_CONFIG = $ServerPlatformConfigPath
  if (Test-Path -LiteralPath $ServerPlatformConfigPath) {
    $ServerPlatformConfig = Get-Content -LiteralPath $ServerPlatformConfigPath -Raw | ConvertFrom-Json
    $ServerEdme = $ServerPlatformConfig.edme
    $ServerAuthMode = if ($ServerEdme -and $ServerEdme.auth_mode) {
      ([string]$ServerEdme.auth_mode).Trim().ToLowerInvariant()
    } else {
      "server"
    }
    $ServerClientDelegated = $ServerEdme -and $ServerAuthMode -eq "client"
    $ServerHasClientCredential = $ServerEdme -and (
      $ServerEdme.single_tenant_bootstrap -or
      $ServerEdme.username -or
      $ServerEdme.password -or
      $ServerEdme.session
    )
    if ($ServerClientDelegated -and $ServerHasClientCredential) {
      throw "MCP Server client authentication config must be credential-free."
    }
  }
}

if ($ValidateOnly) {
  [pscustomobject]@{
    agent_platform_config = $AgentPlatformConfigPath
    mcp_server_platform_config = $ServerPlatformConfigPath
    client_delegated_edme = [bool]$ClientDelegatedEdme
    mcp_server_config_available = $McpServerConfigAvailable
  } | ConvertTo-Json -Compress
  return
}

$PnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
if ($PnpmCommand) {
  $PackageManager = $PnpmCommand.Source
  $FrontendArguments = @("dev", "--host", "127.0.0.1", "--port", "5174")
} else {
  $NpmCommand = Get-Command npm.cmd -ErrorAction Stop
  $PackageManager = $NpmCommand.Source
  $FrontendArguments = @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5174")
}

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

if ($PlatformConfig) {
  if ($PlatformConfig.mcp.enabled -and $McpServerConfigAvailable) {
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
if ($PlatformConfig -and $PlatformConfig.mcp.enabled -and $McpServerConfigAvailable) {
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
