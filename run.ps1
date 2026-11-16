Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
python -m miror
if ($LASTEXITCODE -ne 0) {
    Write-Host "Miror berhenti dengan kode $LASTEXITCODE"
    Read-Host "Tekan Enter"
}
