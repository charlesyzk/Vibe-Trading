[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OfficialSourceDir,
    [Parameter(Mandatory)][string]$OfficialLicenseFile,
    [string]$BuildPython = 'python.exe'
)

# Builder-only orchestration. The resulting unsigned artifact remains a review
# build until application functionality and Windows installation are verified.
$ErrorActionPreference = 'Stop'
$electronRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $electronRoot '..\..')).Path
$source = (Resolve-Path -LiteralPath $OfficialSourceDir).Path
$license = (Resolve-Path -LiteralPath $OfficialLicenseFile).Path
$buildPythonCommand = Get-Command $BuildPython -ErrorAction Stop
$BuildPython = $buildPythonCommand.Source
if (-not $BuildPython -or -not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
    throw 'BuildPython must identify an installed Python executable.'
}
if (-not (Test-Path -LiteralPath $license -PathType Leaf)) {
    throw 'A copy of the official IBKR API license is required.'
}

& $BuildPython (Join-Path $repoRoot 'scripts\install_ibapi_from_official_source.py') --source-dir $source --verify-only
if ($LASTEXITCODE -ne 0) { throw 'Official IBKR API source verification failed.' }

Push-Location (Join-Path $repoRoot 'frontend')
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
}
finally { Pop-Location }

Push-Location $electronRoot
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) { throw 'Desktop dependency installation failed.' }
    & (Join-Path $PSScriptRoot 'build-backend.ps1') -BuildPython $BuildPython -Clean
    & (Join-Path $PSScriptRoot 'build-ibkr-runtime.ps1') -OfficialSourceDir $source `
        -OfficialLicenseFile $license -BuildPython $BuildPython
    & npm run installer:win:paper-review
    if ($LASTEXITCODE -ne 0) { throw 'PAPER review installer build failed.' }
}
finally { Pop-Location }

Write-Host 'Unsigned PAPER review installer is under desktop/electron/release; it is not yet a partner-ready release.'
