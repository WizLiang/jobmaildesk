param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [string]$Version = "",
    [string]$OutputDirectory = ""
)

# $ExePath may point at JobMailDesk.exe inside the onedir build folder or at the
# folder itself; the whole folder (EXE, CLI twin, _internal, icon) is packaged.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectVersion = (& uv run --frozen --project $projectRoot python (Join-Path $PSScriptRoot 'version.py') check).Trim()
if ($LASTEXITCODE -ne 0 -or -not $projectVersion) { throw "Unable to verify project version" }
if (-not $Version) { $Version = $projectVersion }
if ($Version -ne $projectVersion) { throw "Requested package version does not match the source version" }

# Release notes are kept per final version (docs/ACCEPTANCE_v0.7.0.md) while
# the package may be a pre-release (0.7.0rc1): try the exact name first, then
# the base version without the a/b/rc suffix.
$baseVersion = [regex]::Replace($Version, "(a|b|rc)\d+$", "")
$acceptancePath = $null
foreach ($candidate in @("docs\ACCEPTANCE_v$Version.md", "docs\ACCEPTANCE_v$baseVersion.md")) {
    $path = Join-Path $projectRoot $candidate
    if (Test-Path -LiteralPath $path -PathType Leaf) { $acceptancePath = $path; break }
}
if (-not $acceptancePath) {
    throw "Acceptance record not found: docs\ACCEPTANCE_v$Version.md (or v$baseVersion)"
}

$resolvedExe = [System.IO.Path]::GetFullPath($ExePath)
if (Test-Path -LiteralPath $resolvedExe -PathType Container) {
    $buildDir = $resolvedExe
} else {
    if (-not (Test-Path -LiteralPath $resolvedExe -PathType Leaf)) {
        throw "JobMailDesk.exe not found: $resolvedExe"
    }
    $buildDir = Split-Path -Parent $resolvedExe
}
foreach ($required in @("JobMailDesk.exe", "JobMailDesk-cli.exe", "_internal", "JobMailDesk.ico")) {
    if (-not (Test-Path -LiteralPath (Join-Path $buildDir $required))) {
        throw "Build folder is missing $required : $buildDir"
    }
}
foreach ($executable in @('JobMailDesk.exe', 'JobMailDesk-cli.exe')) {
    $builtVersion = (Get-Item -LiteralPath (Join-Path $buildDir $executable)).VersionInfo.ProductVersion
    if ($builtVersion -ne $Version) { throw "Built program version does not match package version: $executable" }
}
if (-not $OutputDirectory) {
    $OutputDirectory = Split-Path -Parent $buildDir
}
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null

$packageName = "JobMailDesk-Core-v$Version-win-x64"
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "jobmaildesk-package-" + [guid]::NewGuid().ToString("N")
)
$packageRoot = Join-Path $temporaryRoot $packageName
$zipPath = Join-Path $resolvedOutput "$packageName.zip"
$checksumPath = "$zipPath.sha256"

try {
    New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
    Copy-Item -LiteralPath $buildDir -Destination (Join-Path $packageRoot "JobMailDesk") -Recurse
    # Keep script literals ASCII-only for Windows PowerShell 5.1, which may
    # decode UTF-8-without-BOM source files using the active ANSI code page.
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\CORE_QUICKSTART.md") -Destination (Join-Path $packageRoot "QUICKSTART.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\DEPENDENCIES.md") -Destination (Join-Path $packageRoot "DEPENDENCIES.zh-CN.md")
    Copy-Item -LiteralPath $acceptancePath -Destination (Join-Path $packageRoot "ACCEPTANCE.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $projectRoot "PRIVACY.md") -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE.md") -Destination $packageRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\LICENSING.md") -Destination (Join-Path $packageRoot "LICENSING.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install-shortcuts.ps1") -Destination (Join-Path $packageRoot "install-shortcuts.ps1")
    # -Path expands the wildcard so the ZIP root holds JobMailDesk\ plus the docs;
    # -LiteralPath would look for an item literally named '*'.
    Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $zipPath -Force
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) { throw "ZIP not produced: $zipPath" }
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $packageName.zip" | Set-Content -LiteralPath $checksumPath -Encoding ascii
    Write-Output $zipPath
    Write-Output $checksumPath
}
finally {
    $resolvedTemporary = [System.IO.Path]::GetFullPath($temporaryRoot)
    $systemTemporary = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($resolvedTemporary.StartsWith($systemTemporary) -and (Test-Path -LiteralPath $resolvedTemporary)) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
    }
}
