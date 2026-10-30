# Release files of one version (used by .github/workflows/release.yml, runnable locally to check a release):
#   NexusAGYmail-win-x64.zip, NexusAGYmail-win-arm64.zip  self-contained single-file NexusAGYmail.exe per architecture
#   nexus-agymail-<version>-win-x64.mcpb                   MCP bundle (manifest.json, icon, server\NexusAGYmail.exe x64)
#   get.ps1                                               the installer, checked and attested like the packages
#   SHA256SUMS.txt                                        SHA-256 of every file above
# Build outputs in ~\.nexus\build\nexusagymail\release; nothing written to the sources.
# Usage: pwsh -File tools/package.ps1 -Version 0.14.0 [-Out <folder>]
param([Parameter(Mandatory)] [ValidatePattern('^\d+\.\d+\.\d+$')] [string]$Version, [string]$Out)
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
$repo = Split-Path $PSScriptRoot
$work = Join-Path $env:USERPROFILE '.nexus\build\nexusagymail\release'
if (-not $Out) { $Out = Join-Path $work 'dist' }

$engine = [IO.File]::ReadAllText((Join-Path $repo 'native\Program.cs'))
if ($engine -notmatch 'public const string Version = "([^"]+)"' -or $Matches[1] -ne $Version) {
    throw "native\Program.cs has version $($Matches[1]), not $Version."
}
foreach ($folder in $work, $Out) { if (Test-Path $folder) { Remove-Item $folder -Recurse -Force } }
New-Item -ItemType Directory -Force $Out | Out-Null

foreach ($rid in 'win-x64', 'win-arm64') {
    $publish = Join-Path $work $rid
    dotnet publish (Join-Path $repo 'native\NexusAGYmail.csproj') -c Release -r $rid --self-contained true `
        -p:PublishSingleFile=true -p:DebugType=none -p:NexusBuild="$work\$rid-build\" -o $publish -v q
    if ($LASTEXITCODE) { throw "dotnet publish $rid failed." }
    $zip = Join-Path $Out "NexusAGYmail-$rid.zip"
    Compress-Archive -LiteralPath (Join-Path $publish 'NexusAGYmail.exe') -DestinationPath $zip
}

# MCP bundle (https://github.com/modelcontextprotocol/mcpb): the x64 engine, also run by Windows on Arm.
$bundle = Join-Path $work 'mcpb'
New-Item -ItemType Directory -Force (Join-Path $bundle 'server') | Out-Null
Copy-Item (Join-Path $work 'win-x64\NexusAGYmail.exe') (Join-Path $bundle 'server')
Copy-Item (Join-Path $repo 'native\plugin\logo.png') (Join-Path $bundle 'icon.png')
$tools = [IO.File]::ReadAllText((Join-Path $repo 'native\tool-contracts.json')) | ConvertFrom-Json
$manifest = [ordered]@{
    manifest_version = '0.3'
    name = 'nexus-agymail'
    display_name = 'NexusAGYmail'
    version = $Version
    description = "Gmail through the official API; sends a draft only after the user's yes."
    long_description = 'Search, read, attachments, drafts, labels and reversible trash of the signed-in Gmail account. ' +
        'Sending needs the exact approval of the draft summary (single-use accord). Sign in once with ' +
        '`NexusAGYmail.exe auth` (your own Google OAuth client; the token stays on this PC, encrypted with DPAPI).'
    author = [ordered]@{ name = 'Julien Piron'; url = 'https://julienpiron.fr' }
    repository = [ordered]@{ type = 'git'; url = 'https://github.com/pironjulien/NexusAGYmail' }
    homepage = 'https://julienpiron.fr/nexusagymail/'
    documentation = 'https://github.com/pironjulien/NexusAGYmail#readme'
    support = 'https://github.com/pironjulien/NexusAGYmail/issues'
    icon = 'icon.png'
    server = [ordered]@{
        type = 'binary'
        entry_point = 'server/NexusAGYmail.exe'
        mcp_config = [ordered]@{ command = '${__dirname}/server/NexusAGYmail.exe'; args = @('--mcp'); env = [ordered]@{} }
    }
    tools = @($tools | ForEach-Object { [ordered]@{ name = $_.name; description = ($_.description -split '\r?\n')[0] } })
    keywords = @('gmail', 'email', 'google', 'antigravity', 'drafts')
    license = 'MIT'
    compatibility = [ordered]@{ platforms = @('win32') }
}
[IO.File]::WriteAllText((Join-Path $bundle 'manifest.json'), ($manifest | ConvertTo-Json -Depth 10) + "`n", [Text.UTF8Encoding]::new($false))
$mcpb = Join-Path $Out "nexus-agymail-$Version-win-x64.mcpb"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($bundle, $mcpb)

Copy-Item (Join-Path $repo 'get.ps1') $Out
Get-ChildItem $Out -File | Sort-Object Name | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $_.Name
} | Set-Content (Join-Path $Out 'SHA256SUMS.txt') -Encoding utf8NoBOM
Get-ChildItem $Out -File | ForEach-Object { '{0,12:N0}  {1}' -f $_.Length, $_.Name }
