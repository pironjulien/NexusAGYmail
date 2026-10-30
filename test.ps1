# Full offline check (xUnit, `dotnet test`): the engine against a fake Gmail API on loopback, sign-in against a fake
# Google, setup in a temporary home. No call to Google, never the real token. Build and test outputs go to
# ~\.nexus\build\nexusagymail. NEXUS_AGYMAIL_TEST_EXE: test that exact executable (what install.ps1 qualifies).
$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
Push-Location $PSScriptRoot
try {
    dotnet test --project tests/NexusAGYmail.Tests.csproj
    if ($LASTEXITCODE) { throw 'Tests failed.' }
} finally { Pop-Location }
Write-Host 'NexusAGYmail: all checks passed.'
