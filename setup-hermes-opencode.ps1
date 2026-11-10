[CmdletBinding()]
param(
    [string]$HermesPath,
    [string]$OpenCodePath,
    [string]$UvPath,
    [string]$PythonPath,
    [switch]$CheckOnly,
    [switch]$Dev,
    [switch]$SmokeTest
)
$ErrorActionPreference = 'Stop'
$setupRoot = $PSScriptRoot
$runtimePath = Join-Path $setupRoot '.runtime'
$toolsPath = Join-Path $setupRoot '.tools'
$nativeHermesRoot = Join-Path $env:LOCALAPPDATA 'hermes'
$pinnedPython = '3.14.9'
$pinnedUv = '0.13.5'

function Resolve-NativeProgram {
    param([string]$ExplicitPath, [string[]]$Names, [string[]]$Candidates)
    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "Executable not found: $ExplicitPath"
        }
        if ([IO.Path]::GetExtension($ExplicitPath) -ne '.exe') {
            throw 'Provide the native .exe, rather than a .cmd or PowerShell wrapper.'
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).ProviderPath
    }
    foreach ($programName in $Names) {
        $program = Get-Command $programName -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -like '*.exe' } | Select-Object -First 1
        if ($program) { return $program.Path }
    }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).ProviderPath
        }
    }
    return $null
}

$savedToolsFile = Join-Path $runtimePath 'tools.json'
$savedTools = if (Test-Path -LiteralPath $savedToolsFile) {
    Get-Content -LiteralPath $savedToolsFile -Raw | ConvertFrom-Json
} else { $null }
$hermesCandidates = @(
    (Join-Path $nativeHermesRoot 'bin\hermes.exe'),
    (Join-Path $env:USERPROFILE '.local\bin\hermes.exe'),
    (Join-Path $env:USERPROFILE '.hermes\bin\hermes.exe')
)
$openCodeCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\ai.opencode.desktop\resources\opencode-cli.exe'),
    (Join-Path $env:USERPROFILE '.opencode\bin\opencode.exe'),
    (Join-Path $env:USERPROFILE '.local\bin\opencode.exe')
)
if ($savedTools) {
    $hermesCandidates = @($savedTools.hermes) + $hermesCandidates
    $openCodeCandidates = @($savedTools.opencode) + $openCodeCandidates
}
$hermesExe = Resolve-NativeProgram $HermesPath @('hermes.exe') $hermesCandidates
$openCodeExe = Resolve-NativeProgram $OpenCodePath @('opencode.exe', 'opencode-cli.exe') $openCodeCandidates
if (-not $openCodeExe) {
    $npmPackage = Join-Path $env:APPDATA 'npm\node_modules\opencode-ai'
    if (Test-Path -LiteralPath $npmPackage) {
        $npmBinary = Get-ChildItem -LiteralPath $npmPackage -Recurse -File -Filter 'opencode.exe' |
            Select-Object -First 1
        if ($npmBinary) { $openCodeExe = $npmBinary.FullName }
    }
}
if (-not $hermesExe) { throw 'Hermes executable not found. Re-run with -HermesPath C:\path\hermes.exe.' }
if (-not $openCodeExe) { throw 'OpenCode executable not found. Re-run with -OpenCodePath C:\path\opencode.exe.' }

$uvCandidates = @((Join-Path $toolsPath "uv-$pinnedUv\uv.exe"))
$pythonCandidates = @()
$hermesTools = Join-Path $nativeHermesRoot 'tools'
if (Test-Path -LiteralPath $hermesTools) {
    $uvCandidates += @(Get-ChildItem -LiteralPath $hermesTools -Directory -Filter 'uv-*' |
        ForEach-Object { Join-Path $_.FullName 'uv.exe' })
    $pythonCandidates += @(Get-ChildItem -LiteralPath $hermesTools -Directory -Filter 'python-3.14*' |
        ForEach-Object { Join-Path $_.FullName 'python.exe' })
}
if ($savedTools) {
    $uvCandidates = @($savedTools.uv) + $uvCandidates
    $pythonCandidates = @($savedTools.python) + $pythonCandidates
}
$uvExe = Resolve-NativeProgram $UvPath @('uv.exe') $uvCandidates
if ($uvExe -and ((& $uvExe --version) -notmatch "^uv $([regex]::Escape($pinnedUv))(\s|$)")) {
    if ($UvPath) { throw "This package pins uv $pinnedUv. The supplied uv has another version." }
    $uvExe = $null
}
$pythonExe = Resolve-NativeProgram $PythonPath @() $pythonCandidates
if ($pythonExe -and ((& $pythonExe -I -c 'import platform; print(platform.python_version())') -ne $pinnedPython)) {
    if ($PythonPath) { throw "This package pins Python $pinnedPython." }
    $pythonExe = $null
}

Write-Host "Hermes:   $hermesExe"
Write-Host "OpenCode: $openCodeExe"
Write-Host "uv:       $(if ($uvExe) { $uvExe } else { 'Will install pinned uv inside .tools' })"
Write-Host "Python:   $(if ($pythonExe) { $pythonExe } else { 'Will install pinned Python inside .tools' })"
if ($CheckOnly) { return }

New-Item -ItemType Directory -Path $runtimePath, $toolsPath -Force | Out-Null
if (-not $uvExe) {
    $architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    $uvArtifact = switch ($architecture) {
        'X64' { @{ Name='uv-x86_64-pc-windows-msvc.zip'; Hash='a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a' } }
        'Arm64' { @{ Name='uv-aarch64-pc-windows-msvc.zip'; Hash='09f47edf90726084a70f9426f6e3ca4f46e9bf7cbf23b1716ec64d60b055fae3' } }
        default { throw "Unsupported Windows architecture: $architecture" }
    }
    $archivePath = Join-Path $toolsPath $uvArtifact.Name
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$pinnedUv/$($uvArtifact.Name)" -OutFile $archivePath
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $uvArtifact.Hash) {
        throw 'uv archive checksum did not match; nothing was executed.'
    }
    $uvInstallPath = Join-Path $toolsPath "uv-$pinnedUv"
    Expand-Archive -LiteralPath $archivePath -DestinationPath $uvInstallPath -Force
    $uvExe = (Get-ChildItem -LiteralPath $uvInstallPath -Recurse -File -Filter 'uv.exe' |
        Select-Object -First 1).FullName
}

$savedEnvironment = @{}
foreach ($settingName in @('UV_CACHE_DIR','UV_NO_CONFIG','UV_PYTHON_INSTALL_DIR','UV_PYTHON_BIN_DIR','UV_PROJECT_ENVIRONMENT')) {
    $savedEnvironment[$settingName] = [Environment]::GetEnvironmentVariable($settingName, 'Process')
}
Push-Location -LiteralPath $setupRoot
try {
    $env:UV_CACHE_DIR = Join-Path $setupRoot '.uv-cache'
    $env:UV_NO_CONFIG = '1'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $setupRoot '.venv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $toolsPath 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $toolsPath 'bin'
    if (-not $pythonExe) {
        & $uvExe python install $pinnedPython --no-bin --no-registry
        if ($LASTEXITCODE -ne 0) { throw 'Project-local Python installation failed.' }
        $pythonExe = (& $uvExe python find --managed-python --no-project $pinnedPython | Select-Object -Last 1).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the project-local Python.' }
    }
    $syncArgs = @('sync','--locked','--inexact','--python',$pythonExe,'--no-python-downloads')
    if (-not $Dev) { $syncArgs += '--no-dev' }
    & $uvExe @syncArgs
    if ($LASTEXITCODE -ne 0) { throw 'Pinned project dependency installation failed.' }
    $resolvedTools = [ordered]@{ hermes=$hermesExe; opencode=$openCodeExe; uv=$uvExe; python=$pythonExe }
    $resolvedTools | ConvertTo-Json | Set-Content -LiteralPath $savedToolsFile -Encoding UTF8
    & (Join-Path $setupRoot '.venv\Scripts\python.exe') -m hermes_gateway.doctor
    if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. See the messages above.' }
    if ($SmokeTest) {
        $smokeOutput = @(& (Join-Path $setupRoot '.venv\Scripts\python.exe') -m hermes_gateway.opencode_launcher --prompt 'Reply exactly SETUP_OK. Do not use tools.')
        $smokeExitCode = $LASTEXITCODE
        $smokeOutput | ForEach-Object { Write-Host $_ }
        $smokeLines = @($smokeOutput | ForEach-Object { $_.ToString().Trim() })
        if ($smokeExitCode -ne 0 -or $smokeLines -notcontains 'SETUP_OK') {
            throw 'Smoke test did not return SETUP_OK. Check model availability, login, and provider quota.'
        }
        $preflightPath = Join-Path $runtimePath 'preflight.json'
        $preflightReport = Get-Content -LiteralPath $preflightPath -Raw | ConvertFrom-Json
        $preflightReport.inference_tested = $true
        $preflightReport | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $preflightPath -Encoding UTF8
    }
    Write-Host "`nSetup complete. Start chatting with: .\hermes-opencode.bat"
} finally {
    foreach ($settingName in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($settingName, $savedEnvironment[$settingName], 'Process')
    }
    Pop-Location
}
