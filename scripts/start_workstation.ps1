param(
    [string]$PythonPath = 'D:\Anaconda_envs\envs\ima-agent\python.exe'
)

$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectPath 'tmp/server.pid'
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener -and -not ((Test-Path -LiteralPath $pidFile) -and
        $listener.OwningProcess -contains [int](Get-Content -LiteralPath $pidFile))) {
    throw 'Port 8000 is occupied by another application.'
}

$dockerReady = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $ErrorActionPreference = 'Continue'
    & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    $dockerReady = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if ($dockerReady) { break }
    if ($attempt -eq 0) {
        Start-Process -FilePath 'C:\Program Files\Docker\Docker\Docker Desktop.exe' -WindowStyle Hidden
    }
    Start-Sleep -Seconds 2
}
if (-not $dockerReady) { throw 'Docker is not ready. Open Docker Desktop and retry.' }

foreach ($container in @('project01-postgres', 'project01-etcd', 'project01-minio', 'project01-milvus')) {
    $existing = & docker ps -a --filter "name=^${container}$" --format '{{.Names}}'
    if ($existing) {
        & docker start $container | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Container startup failed: $container" }
    }
}

# Restore dependency containers even when the Python process survived a Docker
# outage. A listening port alone does not mean the workbench can save tasks.
if ($listener) {
    Write-Output 'Project01 is already running; dependency services checked.'
    return
}

$modelCache = Join-Path $projectPath 'models/paddlex'
if (Test-Path -LiteralPath (Join-Path $modelCache 'official_models')) {
    $env:PADDLE_PDX_CACHE_HOME = $modelCache
    $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = 'True'
}
# The project launcher runs the sole backend.main entrypoint.
& (Join-Path $PSScriptRoot 'start_project.ps1') -PythonPath $PythonPath -Background
