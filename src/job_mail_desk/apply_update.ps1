param([Parameter(Mandatory=$true)][string]$JobFile)
$ErrorActionPreference = 'Stop'
$job = Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
$install = [IO.Path]::GetFullPath($job.install)
$stage = [IO.Path]::GetFullPath($job.stage)
$backup = [IO.Path]::GetFullPath($job.backup)
$parent = [IO.Path]::GetDirectoryName($install)
$movedOld = $false
$movedNew = $false
try {
    if (-not $parent -or $install -eq [IO.Path]::GetPathRoot($install)) { throw 'Invalid installation root' }
    if ([IO.Path]::GetDirectoryName($stage) -ne $parent -or [IO.Path]::GetDirectoryName($backup) -ne $parent) { throw 'Update paths must be siblings' }
    if ([IO.Path]::GetFileName($stage) -notmatch '^\.jobmaildesk-update-[a-f0-9]{32}$' -or [IO.Path]::GetFileName($backup) -notmatch '^\.jobmaildesk-backup-[a-f0-9]{32}$') { throw 'Invalid update directory' }
    foreach ($path in @($install, $stage)) {
        $item = Get-Item -LiteralPath $path
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused' }
        foreach ($name in @('JobMailDesk.exe','JobMailDesk-cli.exe','_internal','JobMailDesk.ico')) {
            if (-not (Test-Path -LiteralPath (Join-Path $path $name))) { throw 'Incomplete program directory' }
        }
    }
    if (Test-Path -LiteralPath $backup) { throw 'Backup already exists' }
    $oldProcess = Get-Process -Id $job.parent_pid -ErrorAction SilentlyContinue
    if ($oldProcess -and -not $oldProcess.WaitForExit(90000)) { throw 'Old application did not exit' }
    # File handles can outlive the window briefly; never kill another process.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            [IO.Directory]::Move($install, $backup)
            $movedOld = $true
            break
        } catch {
            if ($attempt -eq 19) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    [IO.Directory]::Move($stage, $install)
    $movedNew = $true
    Start-Process -FilePath (Join-Path $install 'JobMailDesk.exe') -ArgumentList 'show' -WorkingDirectory $install -WindowStyle Hidden
    @{status='installed'; backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath $job.result -Encoding UTF8 -ErrorAction SilentlyContinue
} catch {
    # Keep both bundles; restore the original directory on a failed replacement.
    if ($movedNew -and (Test-Path -LiteralPath $install)) {
        try { [IO.Directory]::Move($install, $stage) } catch { }
    }
    if ($movedOld -and -not (Test-Path -LiteralPath $install)) {
        try { [IO.Directory]::Move($backup, $install) } catch { }
    }
    @{status='failed'; message='Update failed; use the original program or the preserved backup.'; backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath $job.result -Encoding UTF8
    if (Test-Path -LiteralPath (Join-Path $install 'JobMailDesk.exe')) {
        Start-Process -FilePath (Join-Path $install 'JobMailDesk.exe') -ArgumentList 'show' -WorkingDirectory $install -WindowStyle Hidden
    }
    exit 1
}
