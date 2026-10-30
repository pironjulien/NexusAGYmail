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

function Install-NexusAGYmail {
    $work = Join-Path ([IO.Path]::GetTempPath()) "nexusagymail-$PID"
    New-Item -ItemType Directory -Force -Path $work | Out-Null
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
        $unpacked = Join-Path $work 'package'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unpacked)
        $exe = Join-Path $unpacked 'NexusAGYmail.exe'
        if (-not (Test-Path -LiteralPath $exe)) { throw 'Not a NexusAGYmail package.' }
        Get-ChildItem -LiteralPath $unpacked -Recurse -File | Unblock-File
        $version = ((& $exe --version) -replace '^NexusAGYmail\s+', '').Trim()
        if ($LASTEXITCODE -or $version -notmatch '^\d+\.\d+\.\d+$') { throw "The package does not run on this PC ($Arch)." }
        # One folder per version: a running server keeps its own executable; the new one is used at the next start.
        $target = Join-Path $Root $version
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
        if (-not (Test-Path -LiteralPath $target)) {
            New-Item -ItemType Directory -Force -Path $Root | Out-Null
            Move-Item -LiteralPath $unpacked -Destination $target
        }
        $installed = Join-Path $target 'NexusAGYmail.exe'
        Say "NexusAGYmail $version is in $target." Green
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }

    foreach ($line in @(& $installed setup)) {
        if ($line -like 'NEXUS_PACK_RESULT*') { $script:PackResult = $line } else { Say $line }
    }
    $code = $LASTEXITCODE
    # Older versions no process still runs.
    $running = @(Get-Process -Name NexusAGYmail -ErrorAction SilentlyContinue | ForEach-Object { $_.Path })
    foreach ($old in @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue)) {
        if ($old.FullName -eq $target -or $running -contains (Join-Path $old.FullName 'NexusAGYmail.exe')) { continue }
        Remove-Item -LiteralPath $old.FullName -Recurse -Force -ErrorAction SilentlyContinue
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
