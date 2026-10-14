# Runs only inside a newly created symbol-agent job directory.
param(
    [Parameter(Mandatory=$true)][string]$Tclsh,
    [Parameter(Mandatory=$true)][string]$Script,
    [Parameter(Mandatory=$true)][string]$Part,
    [int]$TimeoutSeconds = 300
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$runDir = Split-Path -Parent $Script
$log = ''
$exitCode = $null
$timedOut = $false
$process = $null
try {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Tclsh
    $info.Arguments = '"' + $Script + '"'
    $info.WorkingDirectory = $runDir
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $timedOut = $true
        $process.Kill()
        $process.WaitForExit()
    }
    $exitCode = $process.ExitCode
    $log = $stdout.Result + "`n[stderr]`n" + $stderr.Result
    if ($timedOut) { $log += "`nCapture execution timed out" }
} catch {
    $log += "`n" + $_.Exception.Message
} finally {
    if ($null -ne $process) { $process.Dispose() }
}
[IO.File]::WriteAllText((Join-Path $runDir 'capture.log'), $log, (New-Object Text.UTF8Encoding($false)))
$files = @{}
foreach ($extension in @('OLB', 'DSN')) {
    $path = Join-Path $runDir ($Part + '.' + $extension)
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $files[$extension] = @{
            size = (Get-Item -LiteralPath $path).Length
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
}
$passed = (-not $timedOut) -and ($exitCode -eq 0) -and
    $log.Contains('===== CAPTURE RESULT: PASS =====') -and
    ($files.Count -eq 2) -and ($files['OLB'].size -gt 0) -and ($files['DSN'].size -gt 0)
$result = @{ passed = [bool]$passed; exit_code = $exitCode; timed_out = $timedOut; files = $files }
$json = ConvertTo-Json -InputObject $result -Depth 4 -Compress
[IO.File]::WriteAllText((Join-Path $runDir 'result.json'), $json, (New-Object Text.UTF8Encoding($false)))
Write-Output $json
