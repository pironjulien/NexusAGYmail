# NexusAGYmail for Windows 10/11 (Windows PowerShell 5.1 or PowerShell 7), no administrator rights, no Python.
# Usual way: the NexusSystem app (Microsoft Store), which runs this file. By hand: download get.ps1 and SHA256SUMS.txt
# from the latest release, check the hash (and `gh attestation verify get.ps1 --repo pironjulien/NexusAGYmail`), then:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\get.ps1          install or update
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\get.ps1 -Auth    then the guided Google sign-in
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\get.ps1 -Remove  Antigravity plugin and program removed
#                                         (your token and OAuth client stay in ~\.nexus\nexus-agymail)
# It downloads the latest release's NexusAGYmail-win-x64.zip or NexusAGYmail-win-arm64.zip (by the PC's architecture),
# checks its SHA-256 against the release's SHA256SUMS.txt before extracting anything (build provenance:
# `gh attestation verify <file> --repo pironjulien/NexusAGYmail`), installs it in ~\.nexus\nexus-agymail\app\<version>
# and runs `NexusAGYmail.exe setup`, which declares the server to Antigravity (plugin). Run it again to update.
# NEXUS_PACK=1 (the Nexus pack): no question, no window; exit code 0 on success; a last "NEXUS_PACK_RESULT ..." line.
# NEXUS_RELEASE_TAG (NexusSystem development channel): install that release (vX.Y.Z or vX.Y.Z-dev.N) instead of the latest.
# For tests only: NEXUS_AGYMAIL_SOURCE (a local zip instead of the latest release) and NEXUS_AGYMAIL_ROOT (another
# install folder). A local zip aimed at the real install folder is refused unless -Real is given.
[CmdletBinding()]
param([switch]$Auth, [switch]$Remove, [switch]$Real)

$ErrorActionPreference = 'Stop'
# Windows PowerShell started from PowerShell 7 inherits its module folders, whose modules it cannot load (Get-FileHash,
# Unblock-File...): keep only its own.
if ($PSVersionTable.PSEdition -ne 'Core') {
    $env:PSModulePath = (($env:PSModulePath -split ';') | Where-Object { $_ -and $_ -notmatch '\\PowerShell\\(7|Modules)' }) -join ';'
}
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is many times slower with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'pironjulien/NexusAGYmail'
$Pack = $env:NEXUS_PACK -eq '1'
$Root = if ($env:NEXUS_AGYMAIL_ROOT) { $env:NEXUS_AGYMAIL_ROOT } else { Join-Path $env:USERPROFILE '.nexus\nexus-agymail\app' }
$Arch = if (($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE) -contains 'ARM64') { 'win-arm64' } else { 'win-x64' }
$Package = "NexusAGYmail-$Arch.zip"

function Say([string]$Text, [string]$Color = 'Gray') { if (-not $Pack) { Write-Host $Text -ForegroundColor $Color } }

function Get-LatestTag {
    # NEXUS_RELEASE_TAG (set by NexusSystem for its development channel): that release instead, a stable vX.Y.Z or a
    # development vX.Y.Z-dev.N build, checked against its own SHA256SUMS.txt like any other.
    if ($env:NEXUS_RELEASE_TAG) {
        if ($env:NEXUS_RELEASE_TAG -notmatch '^v\d+\.\d+\.\d+(-dev\.\d+)?$') { throw "NEXUS_RELEASE_TAG is not a release tag: $env:NEXUS_RELEASE_TAG" }
        return $env:NEXUS_RELEASE_TAG
    }
    # The latest release's tag from the redirect of github.com/<repo>/releases/latest: no GitHub API (60 anonymous
    # requests an hour per IP). The API stays as a fallback.
    try {
        $request = [Net.HttpWebRequest]::Create("https://github.com/$Repo/releases/latest")
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'NexusAGYmail-get'
        $response = $request.GetResponse()
        try { $location = $response.Headers['Location'] } finally { $response.Close() }
        if ($location -match '/releases/tag/(v[0-9][^/?#]*)$') { return $Matches[1] }
    } catch { Write-Verbose "Redirect lookup failed: $($_.Exception.Message)" }
    return (Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases/latest").tag_name
}

function Get-ExpectedHash([byte[]]$Manifest, [string]$Name) {
    $text = [Text.UTF8Encoding]::new($false, $true).GetString($Manifest).TrimStart([char]0xfeff)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $expected = $null
    foreach ($line in ($text -split '\r?\n' | Where-Object { $_.Trim() })) {
        if ($line -cnotmatch '^([0-9a-f]{64})\s+\*?(\S+)\s*$') { throw 'SHA256SUMS.txt has an invalid entry: installation refused.' }
        if (-not $seen.Add($Matches[2])) { throw 'SHA256SUMS.txt lists a file more than once: installation refused.' }
        if ($Matches[2] -ceq $Name) { $expected = $Matches[1] }
    }
    if (-not $expected) { throw "$Name is absent from SHA256SUMS.txt: installation refused." }
    return $expected
}

function Move-StagedFolder([string]$From, [string]$To) {
    # A folder cannot be renamed while any file inside it is open, whoever holds it. This installer never runs or keeps
    # open anything in the staged folder, but antivirus and indexing services open freshly written executables for a
    # moment to scan them: once, on a fresh GitHub Windows runner, the rename failed with "being used by another
    # process" and passed when run again. Only that external, transient hold (sharing, lock or access violation while
    # the staged folder is still there) is waited out, at most 10 s; any other error, or a longer hold, fails.
    $transient = 0x80070020, 0x80070021, 0x80070005  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION, ERROR_ACCESS_DENIED
    for ($attempt = 1; ; $attempt++) {
        try { [IO.Directory]::Move($From, $To); return }
        catch {
            $failure = $_.Exception
            while ($failure.InnerException -and $failure -is [Management.Automation.MethodInvocationException]) { $failure = $failure.InnerException }
            $held = ($failure -is [IO.IOException] -or $failure -is [UnauthorizedAccessException]) -and
                ($transient -contains $failure.HResult) -and (Test-Path -LiteralPath $From) -and -not (Test-Path -LiteralPath $To)
            if (-not $held -or $attempt -ge 40) { throw "Cannot move $From to $($To): $($failure.Message)" }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Get-EngineImages {
    # Image paths of the running NexusAGYmail processes, read by QueryFullProcessImageNameW with
    # PROCESS_QUERY_LIMITED_INFORMATION: unlike Get-Process .Path (empty for an elevated process), it reads every process
    # of this user. Unknown: one runs but cannot be read at all; no folder is then taken as unused.
    if (-not ('NexusAgymailProcessImage' -as [type])) {
        Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text;
public static class NexusAgymailProcessImage {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, int id);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr handle, out uint code);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool QueryFullProcessImageNameW(IntPtr process, uint flags, StringBuilder name, ref uint size);
    // Image path of a process; "" once it has exited; null while it runs but cannot be read.
    public static string Read(int id) {
        IntPtr handle = OpenProcess(0x1000, false, id);  // PROCESS_QUERY_LIMITED_INFORMATION
        if (handle == IntPtr.Zero) return Marshal.GetLastWin32Error() == 87 ? "" : null;  // 87: no such process
        try {
            StringBuilder name = new StringBuilder(32768); uint size = (uint)name.Capacity;
            if (QueryFullProcessImageNameW(handle, 0, name, ref size)) return name.ToString(0, (int)size);
            uint code; return GetExitCodeProcess(handle, out code) && code != 259 ? "" : null;  // 259: STILL_ACTIVE
        } finally { CloseHandle(handle); }
    }
}
'@
    }
    $paths = New-Object Collections.Generic.List[string]; $unknown = $false
    foreach ($process in @(Get-Process -Name NexusAGYmail -ErrorAction SilentlyContinue)) {
        $path = [NexusAgymailProcessImage]::Read($process.Id)
        if ($null -eq $path) { $unknown = $true } elseif ($path) { $paths.Add($path) }
    }
    [pscustomobject]@{ Paths = $paths.ToArray(); Unknown = $unknown }
}

function Test-FolderInUse([string]$Folder, $Images = (Get-EngineImages)) {
    # A folder may still be used when a NexusAGYmail process cannot be read, when one runs from it (its executable is
    # in it), or when its engine.lock is held: every engine holds the one of its folder for its whole life
    # (EngineLock.cs), whatever spelling of the path started it. Windows renames the folder of a running single-file
    # program (only mapped, no file open), so a rename alone cannot tell.
    if ($Images.Unknown) { return $true }
    $prefix = $Folder.TrimEnd('\') + '\'
    if ($Images.Paths | Where-Object { $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }) { return $true }
    $lock = Join-Path $Folder 'engine.lock'
    if (Test-Path -LiteralPath $lock -PathType Leaf) {
        try { [IO.File]::Open($lock, 'Open', 'ReadWrite', 'None').Dispose() } catch { return $true }
    }
    return $false
}

function Test-SameFiles([string]$A, [string]$B) {
    # The same relative file names with the same SHA-256.
    $list = {
        param($Folder)
        $base = (Resolve-Path -LiteralPath $Folder).Path.TrimEnd('\').Length + 1
        @(Get-ChildItem -LiteralPath $Folder -Recurse -File | Sort-Object FullName |
            ForEach-Object { $_.FullName.Substring($base) + '|' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }) -join "`n"
    }
    return (& $list $A) -ceq (& $list $B)
}

function Install-NexusAGYmail {
    $work = Join-Path ([IO.Path]::GetTempPath()) "nexusagymail-$PID"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    # Staged next to the install folder (same volume: the final move is a rename), never inside it: NexusSystem reads
    # the newest NexusAGYmail.exe under app\<version>.
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $staged = Join-Path (Split-Path -Parent $Root) ('staging-' + [guid]::NewGuid().ToString('N'))
    try {
        if ($env:NEXUS_AGYMAIL_SOURCE) {
            $zip = (Resolve-Path -LiteralPath $env:NEXUS_AGYMAIL_SOURCE).Path
            Say "Installing NexusAGYmail from $zip..."
        } else {
            $tag = Get-LatestTag
            $base = "https://github.com/$Repo/releases/download/$tag"
            Say "Downloading NexusAGYmail $tag ($Arch)..."
            $zip = Join-Path $work $Package
            Invoke-WebRequest -UseBasicParsing "$base/$Package" -OutFile $zip
            $manifest = (Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS.txt").RawContentStream.ToArray()
            $expected = Get-ExpectedHash $manifest $Package
            $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($expected -ne $actual) { throw "SHA-256 mismatch for $($Package): the download is not the published file." }
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $staged)
        $exe = Join-Path $staged 'NexusAGYmail.exe'
        if (-not (Test-Path -LiteralPath $exe)) { throw 'Not a NexusAGYmail package.' }
        Get-ChildItem -LiteralPath $staged -Recurse -File | Unblock-File
        # Held by each engine run from the installed folder (EngineLock.cs): the cleanup below sees it in use.
        [IO.File]::WriteAllText((Join-Path $staged 'engine.lock'), '')
        # The version from the executable's version resource, without running it: a program just run from the staged
        # folder is what scanners open right after, when the folder has to be renamed.
        $version = "$((Get-Item -LiteralPath $exe).VersionInfo.ProductVersion)".Split('+')[0].Trim()
        if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Not a NexusAGYmail package (no version).' }
        # One folder per version, never modified once written: a running server keeps reading its own files (an engine
        # loads code from them after it starts), the new version is used at the next start. An existing folder of the
        # same version is kept when it holds the same files; another build of that version (development channel)
        # replaces it only while no process runs from it, by renaming it aside first.
        $target = Join-Path $Root $version
        $moved = $false
        if (-not (Test-Path -LiteralPath $target)) { Move-StagedFolder $staged $target; $moved = $true }
        elseif (-not (Test-SameFiles $staged $target)) {
            if (Test-FolderInUse $target) {
                Say "NexusAGYmail $version is running from $($target): that build stays until it stops (restart Antigravity, then run this again)." Yellow
            } else {
                $retired = Join-Path (Split-Path -Parent $Root) ('retired-' + [guid]::NewGuid().ToString('N'))
                Move-StagedFolder $target $retired
                try { Move-StagedFolder $staged $target; $moved = $true }
                catch { Move-StagedFolder $retired $target; throw }
                Remove-Item -LiteralPath $retired -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        $installed = Join-Path $target 'NexusAGYmail.exe'
        # Run once in place: the package must start on this PC's architecture and report the version it carries.
        $answer = ((& $installed --version) -replace '^NexusAGYmail\s+', '').Trim()
        if ($LASTEXITCODE -or $answer -ne $version) {
            if ($moved) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
            throw "The package does not run on this PC ($Arch)."
        }
        Say "NexusAGYmail $version is in $target." Green
    } finally {
        Remove-Item -LiteralPath $work, $staged -Recurse -Force -ErrorAction SilentlyContinue
    }

    foreach ($line in @(& $installed setup)) {
        if ($line -like 'NEXUS_PACK_RESULT*') { $script:PackResult = $line } else { Say $line }
    }
    $code = $LASTEXITCODE
    # Older versions no process still uses: renamed aside first (refused while a file in them is open, as the lock of
    # an engine starting meanwhile), then deleted.
    $images = Get-EngineImages
    foreach ($old in @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if ($old.FullName -eq $target -or (Test-FolderInUse $old.FullName $images)) { continue }
        $retired = Join-Path (Split-Path -Parent $Root) ('retired-' + [guid]::NewGuid().ToString('N'))
        try { [IO.Directory]::Move($old.FullName, $retired) } catch { continue }
        Remove-Item -LiteralPath $retired -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($Auth -and -not $Pack) {
        # Its own console (questions, browser): Start-Process keeps it attached, outside this function's output.
        $signIn = Start-Process -FilePath $installed -ArgumentList 'auth' -NoNewWindow -Wait -PassThru
        if ($signIn.ExitCode) { $code = $signIn.ExitCode }
    } elseif (-not $Pack) {
        Say "Sign in to Gmail: & `"$installed`" auth  (guided), then restart Antigravity."
    }
    return $code
}

function Remove-NexusAGYmail {
    $plugin = Join-Path $env:USERPROFILE '.gemini\config\plugins\nexus-agymail'
    $config = Join-Path $plugin 'mcp_config.json'
    if (Test-Path -LiteralPath $config) {
        $command = ((Get-Content -LiteralPath $config -Raw | ConvertFrom-Json).mcpServers.'nexus-agymail').command
        if ($command -and ([IO.Path]::GetFileName($command) -eq 'NexusAGYmail.exe' -or $command -match 'uvx')) {
            Remove-Item -LiteralPath $plugin -Recurse -Force
            Say 'Antigravity plugin removed (restart Antigravity).'
        } else { Say "Antigravity plugin $plugin is not ours: left as is." Yellow }
    }
    if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction SilentlyContinue }
    Say 'NexusAGYmail removed. Token and OAuth client stay in ~\.nexus\nexus-agymail (delete that folder to forget them).' Green
    return 0
}

$script:PackResult = $null
$code = 1
try {
    if ($env:NEXUS_AGYMAIL_SOURCE -and -not $env:NEXUS_AGYMAIL_ROOT -and -not $Real) {
        throw 'NEXUS_AGYMAIL_SOURCE (a test package) is set but NEXUS_AGYMAIL_ROOT is not: refusing to install for real. Set NEXUS_AGYMAIL_ROOT to a test folder, or pass -Real.'
    }
    if ($Remove) { $code = Remove-NexusAGYmail } else { $code = Install-NexusAGYmail }
} catch {
    Write-Host "NexusAGYmail: $($_.Exception.Message)" -ForegroundColor Red
    $script:PackResult = 'NEXUS_PACK_RESULT Install=failed'
    $code = 1
}
if ($Pack -and $script:PackResult) { Write-Output $script:PackResult }
exit $code
