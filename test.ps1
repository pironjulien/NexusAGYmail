# Full offline check: lint, format, types, unit tests against a fake Gmail API. Exit code 0 when everything passes.
# Works from any copy of the repository: each checkout gets its own test environment under ~\.nexus\nexus-agymail
# (outside the sources, never the environment of the installed server); nothing is written to the source tree.
# No call to Google: the tests talk to a fake Gmail API and the network is cut.
# Python 3.14 and the locked dependencies must already be in the uv cache: run `uv sync --frozen` once with network.
$ErrorActionPreference = 'Stop'
$nodeExe = Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
if (-not $nodeExe) { throw 'Node.js must already be installed for Pyright; test.ps1 never downloads it.' }
$state = Join-Path $HOME '.nexus\nexus-agymail'
$key = [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes($PSScriptRoot.ToLowerInvariant()))).Replace('-', '').Substring(0, 8)
$env:UV_PROJECT_ENVIRONMENT = Join-Path $state "test-venv-$key"
$env:UV_LINK_MODE = 'copy'
$env:UV_OFFLINE = '1'
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPYCACHEPREFIX = Join-Path $state 'cache\pycache'
$env:RUFF_CACHE_DIR = Join-Path $state 'cache\ruff'
$pythonExe = Join-Path $env:UV_PROJECT_ENVIRONMENT $(if ($IsWindows) { 'Scripts\python.exe' } else { 'bin/python' })
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path $pythonExe)) {
        # System Python first: the one uv installs under %AppData% is virtualized by the Claude desktop app.
        $systemPython = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe' } else { '' }
        if ($systemPython -and (Test-Path $systemPython)) { uv venv --quiet --python $systemPython $env:UV_PROJECT_ENVIRONMENT }
        else { uv venv --quiet --python 3.14 $env:UV_PROJECT_ENVIRONMENT }
        if ($LASTEXITCODE) { throw 'Could not create the test environment offline: run `uv sync --frozen` once with network.' }
    }
    uv sync --frozen --quiet
    if ($LASTEXITCODE) { throw 'Offline dependency sync failed: run `uv sync --frozen` once with network.' }
    & $pythonExe -m ruff check src tests
    if ($LASTEXITCODE) { throw 'ruff reported errors.' }
    & $pythonExe -m ruff format --check src tests
    if ($LASTEXITCODE) { throw 'ruff format: files would be reformatted.' }
    # Run the wheel's bundled Pyright with the existing Node: its Python wrapper can fetch updates or install Node.
    $pyright = & $pythonExe -c 'from importlib.resources import files; print(files("pyright").joinpath("dist", "index.js"))'
    if ($LASTEXITCODE -or -not (Test-Path -LiteralPath $pyright)) { throw 'Bundled Pyright is missing: run `uv sync --frozen` once with network.' }
    & $nodeExe $pyright --project $PSScriptRoot --pythonpath $pythonExe
    if ($LASTEXITCODE) { throw 'pyright reported type errors.' }
    & $pythonExe -m pytest -q -p no:cacheprovider --basetemp (Join-Path $state "cache\pytest-$key")
    if ($LASTEXITCODE) { throw 'Unit tests failed.' }
    $stray = Get-ChildItem $PSScriptRoot -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue
    if ($stray) { throw "__pycache__ found in the sources: $($stray.FullName -join ', ')" }
} finally { Pop-Location }
Write-Host 'NexusAGYmail: all checks passed.'
exit 0
