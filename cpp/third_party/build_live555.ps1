<#
PowerShell wrapper to build live555 using MSYS2 MinGW64 shell on Windows.
Usage: Run this from an elevated or normal PowerShell prompt. It will try to locate MSYS2's bash.
#>
param()

$msysPaths = @("C:\msys64\usr\bin\bash.exe", "C:\msys32\usr\bin\bash.exe")
$bash = $null
foreach ($p in $msysPaths) {
    if (Test-Path $p) { $bash = $p; break }
}

if (-not $bash) {
    # try in PATH
    try { $which = (Get-Command bash -ErrorAction Stop).Definition; if ($which) { $bash = $which } } catch { }
}

if (-not $bash) {
    Write-Error "MSYS2 bash not found. Please install MSYS2 (https://www.msys2.org/) and run this script from MSYS2 MinGW64 shell, or ensure MSYS2's bash is in PATH."
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sh = Join-Path $scriptDir 'build_live555_msys2.sh'

if (-not (Test-Path $sh)) {
    Write-Error "Build script not found: $sh"
    exit 1
}

Write-Host "Invoking MSYS2 bash: $bash --login -i $sh"
& $bash --login -i $sh
