param(
    [int]$ApiPort = 8010,
    [int]$WebPort = 5173
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExecutable = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw 'Missing .venv. Install the Python dependencies described in README.md first.'
}

function Start-BackgroundCommand {
    param(
        [string]$CommandLine,
        [string]$WorkingDirectory
    )

    $ProcessStartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $ProcessStartInfo.FileName = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
    $ProcessStartInfo.Arguments = "/d /s /c `"$CommandLine`""
    $ProcessStartInfo.WorkingDirectory = $WorkingDirectory
    $ProcessStartInfo.UseShellExecute = $false
    $ProcessStartInfo.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($ProcessStartInfo) | Out-Null
}

$ApiOutLog = Join-Path $ProjectRoot 'api.out.log'
$ApiErrLog = Join-Path $ProjectRoot 'api.err.log'
$ApiCommand = "`"$PythonExecutable`" -m uvicorn app.main:app --app-dir services/template-api --host 127.0.0.1 --port $ApiPort > `"$ApiOutLog`" 2> `"$ApiErrLog`""
Start-BackgroundCommand -CommandLine $ApiCommand -WorkingDirectory $ProjectRoot

$ViteExecutable = Join-Path $ProjectRoot 'node_modules\.bin\vite.cmd'
if (-not (Test-Path -LiteralPath $ViteExecutable)) {
    throw 'Missing Vite. Run npm.cmd install --cache .\.npm-cache or bun install first.'
}

$WebRoot = Join-Path $ProjectRoot 'apps\studio-web'
$WebOutLog = Join-Path $ProjectRoot 'web.out.log'
$WebErrLog = Join-Path $ProjectRoot 'web.err.log'
$WebCommand = "`"$ViteExecutable`" --host 127.0.0.1 --port $WebPort > `"$WebOutLog`" 2> `"$WebErrLog`""
Start-BackgroundCommand -CommandLine $WebCommand -WorkingDirectory $WebRoot

Write-Host "Template API: http://127.0.0.1:$ApiPort"
Write-Host "Template Studio: http://127.0.0.1:$WebPort"
