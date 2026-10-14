param(
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][string]$Name,
    [int]$TimeoutSeconds = 90
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($Name -notmatch '^[A-Za-z][A-Za-z0-9_]{0,79}$') { throw 'Invalid design name' }
if ($WorkDir -notmatch '^[A-Za-z]:[\\/]') { throw 'WorkDir must be absolute' }
$root = [IO.Path]::GetFullPath($WorkDir)
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'Missing isolated work directory' }
$exe = 'C:/Cadence/SPB_17.2/tools/bin/allegro.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Missing Allegro executable' }
$design = Join-Path $root ($Name + '.dra')
$runs = @()
foreach ($stage in @('build', 'verify', 'props')) {
    $target = (Join-Path $root ($stage + '_' + $Name.ToLowerInvariant() + '.il')).Replace('\', '/')
    [IO.File]::WriteAllText((Join-Path $root 'allegro.ilinit'), ('load("' + $target + '")' + "`n"), (New-Object Text.UTF8Encoding($false)))
    $stdoutPath = Join-Path $root ($stage + '_stdout.txt')
    $stderrPath = Join-Path $root ($stage + '_stderr.txt')
    $allegroArguments = @('-symbol', 'package', '-nographic', '-noopengl', '-product', 'Allegro_Enterprise_PCB_Designer', '-s', ($stage + '.scr'), ($Name + '.dra'))
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $exe
    $info.Arguments = $allegroArguments -join ' '
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) { $process.Kill(); $process.WaitForExit() }
    $exitCode = $process.ExitCode
    [IO.File]::WriteAllText($stdoutPath, $stdout.Result, (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($stderrPath, $stderr.Result, (New-Object Text.UTF8Encoding($false)))
    $runs += @{stage=$stage; exit_code=$exitCode; timed_out=$timedOut; arguments=$info.Arguments}
    $process.Dispose()
    if ($timedOut -or $exitCode -ne 0) { break }
}
$archive = Join-Path $root 'allegro_results.zip'
$files = @(Get-ChildItem -LiteralPath $root -File | Where-Object { $_.Extension -in @('.dra','.psm','.pad','.log','.txt','.il','.scr','.ilinit') } | Select-Object -ExpandProperty FullName)
Compress-Archive -LiteralPath $files -DestinationPath $archive -Force
$native = @(Get-ChildItem -LiteralPath $root -File | Where-Object { $_.Extension -in @('.dra','.psm','.pad') } | ForEach-Object {
    @{name=$_.Name; size=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
})
ConvertTo-Json -InputObject @{runs=$runs; native_files=$native; archive_sha256=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()} -Depth 5 -Compress
