param(
    [string]$OutputRoot = "",
    [switch]$SkipTests,
    [switch]$SkipSecretScan
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $projectRoot "release"
}
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputRoot)

function Get-SourceRevision {
    param([string]$Root)
    # Same idea as packaging/macos/setup.py:_source_revision(): a stable digest
    # of the exact sources that went into a candidate that has no Git commit.
    $files = @(
        Get-Item -LiteralPath (Join-Path $Root "launcher.py"),
                  (Join-Path $Root "pyproject.toml"),
                  (Join-Path $Root "uv.lock"),
                  (Join-Path $Root "JobMailDesk.spec")
        Get-ChildItem -LiteralPath (Join-Path $Root "src\job_mail_desk") -Recurse -File |
            Where-Object { $_.FullName -notmatch "__pycache__" -and $_.Extension -notin ".pyc", ".pyo" }
        Get-ChildItem -LiteralPath (Join-Path $Root "packaging\windows") -File
    ) | Sort-Object FullName
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = New-Object System.IO.MemoryStream
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Root.Length + 1).Replace("\", "/")
        $nameBytes = [System.Text.Encoding]::UTF8.GetBytes($relative + "`n")
        $stream.Write($nameBytes, 0, $nameBytes.Length)
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $hashBytes = [System.Text.Encoding]::ASCII.GetBytes($hash + "`n")
        $stream.Write($hashBytes, 0, $hashBytes.Length)
    }
    $stream.Position = 0
    return ([System.BitConverter]::ToString($sha.ComputeHash($stream)) -replace "-", "").ToLowerInvariant()
}

Push-Location $projectRoot
try {
    # --frozen: the lock file is the authority (same as CI); never resolve anew here.
    uv sync --frozen --group dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
    if (-not $SkipTests) {
        uv run --frozen pytest -p no:cacheprovider
        if ($LASTEXITCODE -ne 0) { throw "pytest failed with exit code $LASTEXITCODE" }
    }
    if (-not $SkipSecretScan) {
        & (Join-Path $PSScriptRoot "secret-scan.ps1")
        if ($LASTEXITCODE -ne 0) { throw "secret scan failed with exit code $LASTEXITCODE" }
    }
    uv run --frozen pyinstaller --noconfirm --clean JobMailDesk.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    $distDir = Join-Path $projectRoot "dist\JobMailDesk"
    foreach ($required in @("JobMailDesk.exe", "JobMailDesk-cli.exe", "_internal")) {
        if (-not (Test-Path -LiteralPath (Join-Path $distDir $required))) {
            throw "PyInstaller output is missing $required"
        }
    }
    # The tray icon and the shortcut scripts look for the .ico next to the EXE.
    Copy-Item -LiteralPath (Join-Path $projectRoot "packaging\windows\JobMailDesk.ico") -Destination (Join-Path $distDir "JobMailDesk.ico") -Force

    $version = (uv run --frozen python -c "from job_mail_desk import __version__; print(__version__)").Trim()
    $revision = Get-SourceRevision -Root $projectRoot
    $uvVersion = (uv --version).Trim()
    $pythonVersion = (uv run --frozen python -c "import sys; print(sys.version.split()[0])").Trim()
    @(
        "JobMailDesk $version (win-x64)",
        "source_revision=$revision",
        "python=$pythonVersion",
        "uv=$uvVersion",
        "built_at=$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    ) | Set-Content -LiteralPath (Join-Path $distDir "BUILD_INFO.txt") -Encoding utf8

    New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
    $target = Join-Path $resolvedOutput "JobMailDesk"
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Copy-Item -LiteralPath $distDir -Destination $target -Recurse
    Write-Output $target
}
finally {
    Pop-Location
}
