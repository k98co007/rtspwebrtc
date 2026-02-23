<#
Fetch and prepare native dependencies for rtspwebrtc C++ extension.
Usage:
  PowerShell -ExecutionPolicy Bypass -File .\cpp\get_dependencies.ps1

This script will attempt, in order:
- If vcpkg is available (VCPKG_ROOT or vcpkg.exe in PATH), install `libdatachannel` via vcpkg.
- Otherwise, clone `paullouisageneau/libdatachannel` and `live555/live555` into cpp\third_party and attempt a basic CMake build.

This script does not modify your system permanently; it only prepares sources under cpp\third_party.
Building libdatachannel and live555 on Windows may require Visual Studio (MSVC), CMake, Python, and Git.
Refer to the repository README for detailed build instructions if the automated steps fail.
#>

Set-StrictMode -Version Latest

function Check-Command($name) {
    return (Get-Command $name -ErrorAction SilentlyContinue) -ne $null
}

Push-Location -Path (Split-Path -Path $MyInvocation.MyCommand.Definition -Parent)
Set-Location -Path ..\cpp

$third = Join-Path (Get-Location) 'third_party'
if (-not (Test-Path $third)) { New-Item -ItemType Directory -Path $third | Out-Null }

Write-Host "Checking for vcpkg..."
$vcpkgRoot = $env:VCPKG_ROOT
if (-not $vcpkgRoot) {
    $vcpkgExe = Get-Command vcpkg -ErrorAction SilentlyContinue
    if ($vcpkgExe) { $vcpkgRoot = Split-Path $vcpkgExe.Path -Parent }
}

if ($vcpkgRoot) {
    Write-Host "Using vcpkg at: $vcpkgRoot"
    $vcpkg = Join-Path $vcpkgRoot 'vcpkg.exe'
    if (-not (Test-Path $vcpkg)) {
        Write-Warning "vcpkg.exe not found under VCPKG_ROOT; please ensure vcpkg is installed."
    } else {
        Write-Host "Installing libdatachannel via vcpkg (x64-windows)..."
        & $vcpkg install libdatachannel:x64-windows | Write-Host
        Write-Host "vcpkg install finished. You can now run build_cpp.ps1 with -VcpkgRoot $vcpkgRoot"
        Pop-Location
        return
    }
}

Write-Host "vcpkg not available or failed; falling back to cloning sources into cpp/third_party"

function Clone-IfMissing($repoUrl, $targetDir) {
    if (-not (Test-Path $targetDir)) {
        Write-Host "Cloning $repoUrl -> $targetDir"
        git clone $repoUrl $targetDir
        if ($LASTEXITCODE -ne 0) { Write-Warning "git clone failed for $repoUrl"; return $false }
    } else {
        Write-Host "$targetDir already exists; skipping clone"
    }
    return $true
}

$libdata_dir = Join-Path $third 'libdatachannel'
$live555_dir = Join-Path $third 'live555'

if (-not (Check-Command git)) {
    Write-Error "git not found in PATH. Please install Git and re-run this script."; Pop-Location; exit 2
}

# Clone libdatachannel and live555 (fallback)
Clone-IfMissing 'https://github.com/paullouisageneau/libdatachannel.git' $libdata_dir | Out-Null

# Try git clone for live555; if it fails, download the official tarball from live555.com
if (-not (Clone-IfMissing 'https://github.com/live555/live555.git' $live555_dir)) {
    Write-Warning "git clone for live555 failed or repo not available; attempting download from live555.com"
    $tarUrl = 'http://www.live555.com/liveMedia/public/live555-latest.tar.gz'
    $outTar = Join-Path $third 'live555-latest.tar.gz'
    try {
        Write-Host "Downloading live555 tarball from $tarUrl..."
        Invoke-WebRequest -Uri $tarUrl -OutFile $outTar -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Warning "Failed to download live555 tarball: $_"
    }

    if (Test-Path $outTar) {
        Write-Host "Extracting $outTar to $third"
        try {
            # Use tar (available on modern Windows) to extract .tar.gz
            tar -xzf $outTar -C $third
            # Move extracted folder (likely named live) to target dir if needed
            $extracted = Get-ChildItem $third | Where-Object { $_.PSIsContainer -and ($_.Name -match 'live') } | Select-Object -First 1
            if ($extracted) {
                $dest = $live555_dir
                if (-not (Test-Path $dest)) { Rename-Item $extracted.FullName $dest -ErrorAction SilentlyContinue }
            }
        } catch {
            Write-Warning "Extraction failed: $_"
        }
    } else {
        Write-Warning "live555 tarball not present; cannot provide live555 sources automatically."
    }
}

Write-Host "Sources cloned into: $third"
Write-Host "Attempting basic CMake configure for libdatachannel (may require additional steps)"

$buildDir = Join-Path $libdata_dir 'build'
if (-not (Test-Path $buildDir)) { New-Item -ItemType Directory -Path $buildDir | Out-Null }
Push-Location $buildDir

if (-not (Check-Command cmake)) { Write-Error "CMake not found in PATH. Install CMake and re-run."; Pop-Location; Pop-Location; exit 3 }

Write-Host "Configuring libdatachannel (Release, x64)..."
& cmake .. -DCMAKE_BUILD_TYPE=Release -A x64
if ($LASTEXITCODE -ne 0) { Write-Warning "CMake configure failed for libdatachannel. See output above."; Pop-Location; Pop-Location; return }

Write-Host "Attempting to build libdatachannel (this may take several minutes)..."
& cmake --build . --config Release
if ($LASTEXITCODE -ne 0) { Write-Warning "Build failed for libdatachannel. See output above." }

Pop-Location
Pop-Location

Write-Host "Dependency fetch script finished. If builds failed, follow each project's README for platform-specific instructions."
