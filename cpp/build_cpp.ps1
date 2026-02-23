param(
    [string]$VcpkgRoot = $env:VCPKG_ROOT,
    [switch]$UseSystemLibs
)

function Check-Command($name) {
    $which = Get-Command $name -ErrorAction SilentlyContinue
    return $which -ne $null
}

Write-Host "Installing Python headers (pybind11)..."
python -m pip install --user pybind11 | Out-Null

if (-not (Check-Command cmake)) {
    Write-Error "CMake not found in PATH. Please install CMake and re-run the script."
    exit 1
}

Push-Location -Path (Split-Path -Path $MyInvocation.MyCommand.Definition -Parent)
Set-Location -Path ..\
Set-Location -Path cpp

if (-not (Test-Path -Path build)) { New-Item -ItemType Directory -Path build | Out-Null }
Set-Location -Path build

$cmakeExtra = ""
if ($VcpkgRoot) {
    $toolchain = Join-Path $VcpkgRoot "scripts\buildsystems\vcpkg.cmake"
    if (Test-Path $toolchain) {
        $cmakeExtra += " -DCMAKE_TOOLCHAIN_FILE=`"$toolchain`""
        Write-Host "Using vcpkg toolchain at $toolchain"
        # Optionally install libdatachannel via vcpkg if available
        $vcpkgExe = Join-Path $VcpkgRoot "vcpkg.exe"
        if (Test-Path $vcpkgExe) {
            Write-Host "Installing libdatachannel via vcpkg (may take a while)..."
            & $vcpkgExe install libdatachannel:x64-windows | Write-Host
        }
    } else {
        Write-Warning "vcpkg toolchain not found at $toolchain; continuing without it."
    }
}

if ($UseSystemLibs) {
    $cmakeExtra += " -DUSE_SYSTEM_LIBS=ON"
} else {
    $cmakeExtra += " -DUSE_SYSTEM_LIBS=OFF"
}

Write-Host "Configuring CMake..."
& cmake .. $cmakeExtra
if ($LASTEXITCODE -ne 0) { Write-Error "CMake configuration failed"; exit 2 }

Write-Host "Building..."
& cmake --build . --config Release
if ($LASTEXITCODE -ne 0) { Write-Error "Build failed"; exit 3 }

# Locate built module
Write-Host "Locating built extension..."
$pyd = Get-ChildItem -Path . -Recurse -Filter "rtspwebrtc_cpp*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pyd) {
    $so = Get-ChildItem -Path . -Recurse -Filter "rtspwebrtc_cpp*.so" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($so) { $pyd = $so }
}

if ($pyd) {
    Write-Host "Built extension: $($pyd.FullName)"
    Write-Host "To use it in Python, add its directory to PYTHONPATH or copy the file into your project root."
    Write-Host "Example (PowerShell):"
    Write-Host "  $env:PYTHONPATH += ';' + '$(Split-Path -Path $($pyd.FullName) -Parent)'"
} else {
    Write-Warning "Could not locate built extension file. Build may have produced a different artifact name."
}

Pop-Location
