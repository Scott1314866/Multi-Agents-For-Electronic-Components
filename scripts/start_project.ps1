param(
    [string]$PythonPath = 'D:\Anaconda_envs\envs\ima-agent\python.exe',
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python interpreter not found: $PythonPath"
}

Push-Location -LiteralPath $projectPath
try {
    $modelCache = Join-Path $projectPath 'models/paddlex'
    if (Test-Path -LiteralPath (Join-Path $modelCache 'official_models')) {
        $env:PADDLE_PDX_CACHE_HOME = $modelCache
        $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = 'True'
    }
    # backend.main is the sole application entrypoint; -s isolates environment packages.
    & $PythonPath -s -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; from backend.main import app; print('Runtime dependency check passed')"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency check failed' }
    if ($Background) {
        $logPath = Join-Path $projectPath 'tmp'
        New-Item -ItemType Directory -Path $logPath -Force | Out-Null
        # Flush logs immediately; UTF-8 also handles OCR diameter/engineering symbols.
        $serverProcess = Start-Process -FilePath $PythonPath -ArgumentList '-s','-u','-X','utf8','-m','backend.main' -WorkingDirectory $projectPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logPath 'server.stdout.log') -RedirectStandardError (Join-Path $logPath 'server.stderr.log') -PassThru
        $serverProcess.Id | Set-Content -LiteralPath (Join-Path $logPath 'server.pid')
        Write-Output "Server PID: $($serverProcess.Id)"
        Write-Output 'Workbench: http://127.0.0.1:8000/api/v1/workbench/ui'
        Write-Output 'STEP page: http://127.0.0.1:8000/api/v1/step/ui'
        Write-Output 'Symbol page: http://127.0.0.1:8000/api/v1/symbol/ui'
        Write-Output 'PCB page: http://127.0.0.1:8000/api/v1/pcb/ui'
        Write-Output 'Listening: 0.0.0.0:8000 (use this computer''s LAN IP from another device)'
        Get-NetIPConfiguration | Where-Object {
            $_.NetAdapter.Status -eq 'Up' -and $_.NetAdapter.HardwareInterface -and $_.IPv4DefaultGateway
        } | ForEach-Object {
            foreach ($projectLanAddress in $_.IPv4Address.IPAddress) {
                Write-Output "LAN workbench: http://${projectLanAddress}:8000/api/v1/workbench/ui"
                Write-Output "LAN PCB page: http://${projectLanAddress}:8000/api/v1/pcb/ui"
            }
        }
    } else {
        & $PythonPath -s -u -X utf8 -m backend.main
        if ($LASTEXITCODE -ne 0) { throw "Backend exited with code $LASTEXITCODE" }
    }
} finally {
    Pop-Location
}
