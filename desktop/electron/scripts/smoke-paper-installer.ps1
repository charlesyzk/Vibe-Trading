[CmdletBinding()]
param([Parameter(Mandatory)][string]$Installer, [string]$TestRoot = '')

# Disposable Windows runner only. This checks installation, launch and
# same-version reinstall without connecting to Gateway or sending an order.
$ErrorActionPreference = 'Stop'
$electronRoot = Split-Path -Parent $PSScriptRoot
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$testRoot = if ($TestRoot) { [IO.Path]::GetFullPath($TestRoot) } else { Join-Path $env:TEMP ("vibe-paper-install-" + [Guid]::NewGuid().ToString('N')) }
if (Test-Path -LiteralPath $testRoot) { throw 'The disposable test root must not already exist.' }
$installDir = Join-Path $testRoot 'app'
$stateDir = Join-Path $testRoot 'profile-state'
New-Item -ItemType Directory -Path $testRoot, $stateDir -Force | Out-Null
$env:VIBE_TRADING_HOME = $stateDir
$env:VIBE_INSTALL_BACKEND = Join-Path $installDir 'resources\backend'

function Install-ReviewPackage {
    $process = Start-Process -FilePath $installerPath -ArgumentList @('/S', "/D=$installDir") -PassThru
    # Cold extraction and same-version replacement of the embedded runtime can
    # take several minutes on shared Windows runners.
    if (-not $process.WaitForExit(720000)) {
        $process.Kill()
        throw 'Installer exceeded the twelve-minute timeout.'
    }
    if ($process.ExitCode -ne 0) { throw "Installer failed with exit code $($process.ExitCode)." }
    $appPath = Join-Path $installDir 'Vibe-Trading Desktop (Unofficial Community Build).exe'
    if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) { throw 'Installed application executable is missing.' }
    node --input-type=module -e "import {verifyPaperRuntime} from './scripts/verify-paper-runtime.mjs'; await verifyPaperRuntime(process.env.VIBE_INSTALL_BACKEND)"
    if ($LASTEXITCODE -ne 0) { throw 'Installed PAPER runtime layout verification failed.' }
    $env:VIBE_TRADING_DESKTOP_ELECTRON_EXECUTABLE = $appPath
    node scripts/smoke-parent-death.mjs
    if ($LASTEXITCODE -ne 0) { throw 'Installed application startup or cleanup failed.' }
    # The parent-death probe intentionally kills only Electron's main process.
    # Reap any orphaned renderer processes belonging to this disposable test
    # installation before asking NSIS to replace the executable on reinstall.
    $remaining = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $appPath })
    foreach ($child in $remaining) {
        Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($remaining.Count -gt 0) {
        Write-Host "Cleaned $($remaining.Count) orphaned test installation process(es)."
    }
}

Push-Location $electronRoot
try {
    Install-ReviewPackage
    $marker = Join-Path $stateDir 'reinstall-preservation.txt'
    [IO.File]::WriteAllText($marker, 'preserve-profile-state', [Text.UTF8Encoding]::new($false))
    Install-ReviewPackage
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -Raw -LiteralPath $marker) -ne 'preserve-profile-state') {
        throw 'Same-version reinstall did not preserve the test profile state.'
    }
    Write-Host 'Clean install, launch, same-version reinstall and profile-state preservation passed.'
}
finally { Pop-Location }
