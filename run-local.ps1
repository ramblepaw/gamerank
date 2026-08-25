# Run GameRank locally on Windows. Nothing touches the NAS.
# Data goes in .\data\, secrets are read from .\.env (gitignored).
$ErrorActionPreference = 'Stop'

if (-not (Test-Path .\.venv)) {
    Write-Host 'Creating virtualenv...'
    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

if (Test-Path .\.env) {
    Get-Content .\.env | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name = $matches[1]
            $value = $matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "env:$name" -Value $value
        }
    }
    Write-Host 'Loaded .env' -ForegroundColor DarkGray
} else {
    Write-Host 'No .env found - copy .env.example to .env for IGDB art.' -ForegroundColor Yellow
}

$env:GRT_DATA_DIR = Join-Path $PSScriptRoot 'data'
if (-not $env:GRT_SECRET) { $env:GRT_SECRET = 'local-dev-secret' }

Write-Host ''
Write-Host '  GameRank running at http://localhost:8099' -ForegroundColor Green
Write-Host '  Sign in as Admin with no password. Ctrl+C to stop.'
Write-Host ''

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8099
