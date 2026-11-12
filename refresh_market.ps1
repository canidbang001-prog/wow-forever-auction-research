param([string]$Python = "")
$ErrorActionPreference = "Stop"
if (-not $Python) {
    $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $Python = if (Test-Path -LiteralPath $BundledPython) { $BundledPython } else { "python" }
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python import_auctionator.py --config config.forever.json
    if ($LASTEXITCODE -ne 0) { throw "Auctionator import failed." }
    & $Python market_dashboard.py --config config.forever.json
    if ($LASTEXITCODE -ne 0) { throw "Dashboard generation failed." }
    Write-Output "Open reports/market.html to view the updated market."
} finally {
    Pop-Location
}
