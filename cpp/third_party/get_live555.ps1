#!/usr/bin/env pwsh
# PowerShell helper to attempt downloading live555. Use WSL for building on Windows.
# Usage: .\get_live555.ps1
param()

$dest = Join-Path $PSScriptRoot "live555"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$tmp = Join-Path $env:TEMP "live555.tar.gz"

$urls = @(
    'https://download.live555.com/live555-latest.tar.gz',
    'https://live555.s3.amazonaws.com/live555-latest.tar.gz',
    'https://github.com/rg3/live555/archive/refs/heads/master.tar.gz',
    'https://gitlab.com/live555/live555/-/archive/master/live555-master.tar.gz'
)

# Allow extra URLs via env var LIVE555_URLS (comma-separated)
if ($env:LIVE555_URLS) {
    $extra = $env:LIVE555_URLS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
    $urls = $urls + $extra
}

$downloaded = $false
foreach ($u in $urls) {
    Write-Host "Trying: $u"
    try {
        Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -ErrorAction Stop
        if (Test-Path $tmp) {
            Write-Host "Downloaded to $tmp"
            $downloaded = $true
            break
        }
    } catch {
        Write-Host "Download failed: $($_.Exception.Message)"
    }
}

# If we couldn't download an archive, attempt git clone only if git exists
if (-not $downloaded) {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "Failed to download archive automatically. Trying git clone fallbacks..."
        $gitUrls = @(
            # Note: upstream may not host a public git mirror; these are optional fallbacks
            'https://github.com/rg3/live555.git',
            'https://github.com/live555/live555.git'
        )
        foreach ($g in $gitUrls) {
            Write-Host "Attempting git clone: $g"
            $tmpDir = Join-Path $dest 'live555-src'
            if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue }
            try {
                git clone --depth=1 $g $tmpDir 2>&1 | Write-Host
                if (Test-Path $tmpDir) {
                    # move contents into $dest/src
                    $dstSrc = Join-Path $dest 'src'
                    if (-not (Test-Path $dstSrc)) { New-Item -ItemType Directory -Path $dstSrc | Out-Null }
                    Get-ChildItem -Path $tmpDir -Force | ForEach-Object { Move-Item -Path $_.FullName -Destination $dstSrc -Force }
                    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
                    $downloaded = $true
                    break
                }
            } catch {
                Write-Host "git clone failed: $($_.Exception.Message)"
                if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue }
            }
        }
    }
}

if (-not $downloaded) {
    Write-Error "Failed to obtain live555 automatically.\nPlease download live555-latest.tar.gz from https://www.live555.com/liveMedia/ and extract it into: $dest"
    exit 1
}

# If we downloaded a tarball, extract it
if (Test-Path $tmp) {
    try {
        tar -xf $tmp -C $dest --strip-components=1
        Write-Host "Extracted archive into: $dest"
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    } catch {
        Write-Error "Failed to extract archive: $($_.Exception.Message)\nPlease extract live555-latest.tar.gz into $dest manually."
        exit 1
    }
} else {
    Write-Host "No tarball to extract; repository sources placed under: $dest/src"
}

Write-Host "NOTE: Building live555 on native Windows is non-trivial. Open WSL (recommended) and run:"
Write-Host "  bash $(Join-Path $PSScriptRoot 'get_live555.sh')"
Write-Host "Or follow live555 build instructions in $dest."
