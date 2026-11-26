$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $backendRoot
try {
    python -m unittest discover -s tests -t . -v
    if ($LASTEXITCODE -ne 0) { throw 'API tests failed' }
} finally {
    Pop-Location
}
