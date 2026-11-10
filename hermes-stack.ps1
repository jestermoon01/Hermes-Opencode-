[CmdletBinding()]
param(
    [string]$Prompt,
    [switch]$GatewayOnly,
    [string]$Toolsets,
    [switch]$OpenCode,
    [switch]$LMStudio
)
$ErrorActionPreference = 'Stop'
$stackRoot = $PSScriptRoot
$toolsRecord = Join-Path $stackRoot '.runtime\tools.json'
if (-not (Test-Path -LiteralPath $toolsRecord)) {
    & (Join-Path $stackRoot 'setup-hermes-opencode.ps1')
}
$resolvedTools = Get-Content -LiteralPath $toolsRecord -Raw | ConvertFrom-Json
$uvPath = $resolvedTools.uv
$pythonPath = $resolvedTools.python
if (-not (Test-Path -LiteralPath $uvPath) -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Setup paths are stale. Re-run setup-hermes-opencode.ps1 on this laptop.'
}
$previousUvCache = $env:UV_CACHE_DIR
$previousNoConfig = $env:UV_NO_CONFIG
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
Push-Location -LiteralPath $stackRoot
try {
    $env:UV_CACHE_DIR = Join-Path $stackRoot '.uv-cache'
    $env:UV_NO_CONFIG = '1'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $stackRoot '.venv'
    & $uvPath sync --locked --inexact --no-dev --python $pythonPath --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Project dependency synchronization failed.' }
    $stackModule = if ($LMStudio) { 'hermes_gateway.launcher' } else { 'hermes_gateway.opencode_launcher' }
    $stackArgs = @('-m', $stackModule)
    if ($PSBoundParameters.ContainsKey('Prompt')) { $stackArgs += @('--prompt', $Prompt) }
    if ($GatewayOnly) { $stackArgs += '--gateway-only' }
    if ($Toolsets -and $LMStudio) { $stackArgs += @('--toolsets', $Toolsets) }
    & (Join-Path $stackRoot '.venv\Scripts\python.exe') @stackArgs
    $stackExitCode = $LASTEXITCODE
} finally {
    $env:UV_CACHE_DIR = $previousUvCache
    $env:UV_NO_CONFIG = $previousNoConfig
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
    Pop-Location
}
if ($stackExitCode -ne 0) { throw "Hermes stack exited with code $stackExitCode" }
