param(
    [int]$ApiPort = 8010,
    [int]$WebPort = 5173
)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExecutable = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw 'Missing .venv. Install the Python dependencies described in README.md first.'
}

$BunExecutable = $null
$bunCandidates = @(
    (Get-Command bun.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    (Join-Path $env:APPDATA 'npm\node_modules\bun\bin\bun.exe'),
    (Join-Path $env:USERPROFILE '.bun\bin\bun.exe')
)
foreach ($candidate in $bunCandidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        $BunExecutable = $candidate
        break
    }
}
if (-not $BunExecutable) {
    throw 'Missing bun.exe. Install Bun and ensure it is on PATH, or install the npm package `bun`.'
}

Start-Process -FilePath $PythonExecutable `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'services/template-api', '--host', '127.0.0.1', '--port', $ApiPort) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ProjectRoot 'api.out.log') `
    -RedirectStandardError (Join-Path $ProjectRoot 'api.err.log')

Start-Process -FilePath $BunExecutable `
    -ArgumentList @('--cwd', 'apps/studio-web', 'dev', '--host', '127.0.0.1', '--port', $WebPort) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ProjectRoot 'web.out.log') `
    -RedirectStandardError (Join-Path $ProjectRoot 'web.err.log')

Write-Host "Template API: http://127.0.0.1:$ApiPort"
Write-Host "Template Studio: http://127.0.0.1:$WebPort"
