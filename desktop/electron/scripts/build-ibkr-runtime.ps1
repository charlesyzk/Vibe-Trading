[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OfficialSourceDir,
    [Parameter(Mandatory)][string]$OfficialLicenseFile,
    [string]$BuildPython = 'python.exe'
)

$ErrorActionPreference = 'Stop'
$electronRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $electronRoot '..\..')).Path
$backendRoot = Join-Path $electronRoot 'runtime\backend'
$target = Join-Path $backendRoot 'ibkr-runtime'
$archive = Join-Path $electronRoot '.cache\python\python-3.12.10-embed-amd64.zip'
$expectedArchiveHash = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$source = (Resolve-Path -LiteralPath $OfficialSourceDir).Path
$license = (Resolve-Path -LiteralPath $OfficialLicenseFile).Path
if (-not (Test-Path -LiteralPath (Join-Path $backendRoot 'python.exe'))) {
    throw 'Assemble the Windows backend first with build-backend.ps1.'
}
if (Test-Path -LiteralPath $target) {
    throw "IBKR runtime destination already exists; build into a fresh backend: $target"
}
if (-not (Test-Path -LiteralPath $archive) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant() -ne $expectedArchiveHash) {
    throw 'The checksum-pinned Python 3.12.10 embedded archive is missing or invalid.'
}
if (-not (Test-Path -LiteralPath $license -PathType Leaf)) {
    throw 'The official SDK license file is required for this build.'
}
& $BuildPython (Join-Path $repoRoot 'scripts\install_ibapi_from_official_source.py') --source-dir $source --verify-only
if ($LASTEXITCODE -ne 0) { throw 'Official SDK source verification failed.' }

New-Item -ItemType Directory -Path $target | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $target
$site = Join-Path $target 'Lib\site-packages'
New-Item -ItemType Directory -Path $site -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $target 'python312._pth'), "python312.zip`r`n.`r`nLib\site-packages`r`nimport site`r`n", [Text.UTF8Encoding]::new($false))
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: `
    --target $site --requirement (Join-Path $electronRoot 'requirements-ibkr-windows-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing locked IBKR runtime dependencies failed.' }
& robocopy (Join-Path $source 'ibapi') (Join-Path $site 'ibapi') /E /XD __pycache__ /XF '*.pyc' '*.pyo' /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw 'Copying verified SDK source failed.' }
& robocopy (Join-Path $repoRoot 'ibkr-runtime\ibkr_runtime') (Join-Path $site 'ibkr_runtime') /E /XD __pycache__ /XF '*.pyc' '*.pyo' /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw 'Copying isolated runtime source failed.' }
Copy-Item -LiteralPath $license -Destination (Join-Path $target 'IBAPI-LICENSE.txt')

$runtimePython = Join-Path $target 'python.exe'
& $runtimePython -c "import ibapi, google.protobuf, ibkr_runtime.worker; assert ibapi.get_version_string() == '10.50.2'; assert google.protobuf.__version__ == '5.29.5'; print('isolated IBKR runtime imports OK')"
if ($LASTEXITCODE -ne 0) { throw 'Isolated IBKR runtime import smoke test failed.' }
Write-Host "Isolated IBKR runtime assembled: $target"
