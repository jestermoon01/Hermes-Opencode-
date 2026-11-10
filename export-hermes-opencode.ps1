[CmdletBinding()]
param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath $PSScriptRoot).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $sourceRoot 'dist\Hermes-Opencode' }
$exportRoot = [IO.Path]::GetFullPath($OutputDirectory)
$rootPrefix = $sourceRoot.TrimEnd('\') + '\'
if (-not $exportRoot.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Export must stay inside this project folder.'
}
if (Test-Path -LiteralPath $exportRoot) {
    if (Get-ChildItem -LiteralPath $exportRoot -Force | Select-Object -First 1) {
        throw 'Export destination is not empty. Choose a new output directory.'
    }
}
$manifest = Get-Content -LiteralPath (Join-Path $sourceRoot 'publish-manifest.json') -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Path $exportRoot -Force | Out-Null
foreach ($relativePath in $manifest) {
    $sourceFile = [IO.Path]::GetFullPath((Join-Path $sourceRoot $relativePath))
    $targetFile = [IO.Path]::GetFullPath((Join-Path $exportRoot $relativePath))
    if (-not $sourceFile.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $targetFile.StartsWith(($exportRoot.TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe manifest entry: $relativePath"
    }
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($targetFile)) -Force | Out-Null
    Copy-Item -LiteralPath $sourceFile -Destination $targetFile
}
$archivePath = $exportRoot + '.zip'
Compress-Archive -Path (Join-Path $exportRoot '*') -DestinationPath $archivePath -Force
Write-Host "Clean source: $exportRoot"
Write-Host "Archive:      $archivePath"
