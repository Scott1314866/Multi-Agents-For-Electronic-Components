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
    # -s prevents user-wide packages from overriding the conda environment.
    & $PythonPath -s -c "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; from backend.main import app; print('Runtime dependency check passed')"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency check failed' }
    if ($Background) {
        $logPath = Join-Path $projectPath 'tmp'
        New-Item -ItemType Directory -Path $logPath -Force | Out-Null
        # Flush logs immediately; UTF-8 also handles OCR diameter/engineering symbols.
        $serverProcess = Start-Process -FilePath $PythonPath -ArgumentList '-s','-u','-X','utf8','-m','backend.main' -WorkingDirectory $projectPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logPath 'server.stdout.log') -RedirectStandardError (Join-Path $logPath 'server.stderr.log') -PassThru
        $serverProcess.Id | Set-Content -LiteralPath (Join-Path $logPath 'server.pid')
        Write-Output "Server PID: $($serverProcess.Id)"
        Write-Output 'Project page: http://127.0.0.1:8000/api/v1/step/ui'
    } else {
        & $PythonPath -s -u -X utf8 -m backend.main
        if ($LASTEXITCODE -ne 0) { throw "Backend exited with code $LASTEXITCODE" }
    }
} finally {
    Pop-Location
}
