param(
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z][A-Za-z0-9_]{0,79}$')][string]$Name,
    [Parameter(Mandatory=$true)][ValidateSet('build','verify','props')][string]$Stage,
    [Parameter(Mandatory=$true)][string]$Exe,
    [ValidatePattern('^[A-Za-z0-9_]+$')][string]$Product = 'Allegro_Enterprise_PCB_Designer',
    [ValidateRange(1,1800)][int]$TimeoutSeconds = 600
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($WorkDir -notmatch '^[A-Za-z]:[\\/]') { throw 'WorkDir must be absolute' }
$root = [IO.Path]::GetFullPath($WorkDir)
if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'Missing isolated work directory' }
if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { throw 'Missing Allegro executable' }
$target = (Join-Path $root ($Stage + '_' + $Name.ToLowerInvariant() + '.il')).Replace('\','/')
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw 'Missing SKILL script' }
$log = Join-Path $root ($Stage + '.log')
if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log }
[IO.File]::WriteAllText((Join-Path $root 'allegro.ilinit'), ('load("' + $target + '")' + "`n"), (New-Object Text.UTF8Encoding($false)))
$info = New-Object Diagnostics.ProcessStartInfo
$info.FileName = $Exe
$info.Arguments = @('-symbol','package','-nographic','-noopengl','-product',$Product,'-s',($Stage + '.scr'),($Name + '.dra')) -join ' '
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
[IO.File]::WriteAllText((Join-Path $root ($Stage + '_stdout.txt')), $stdout.Result, (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $root ($Stage + '_stderr.txt')), $stderr.Result, (New-Object Text.UTF8Encoding($false)))
$process.Dispose()
$archive = Join-Path $root ($Stage + '_results.zip')
$files = @(Get-ChildItem -LiteralPath $root -File | Where-Object { $_.Extension -in @('.dra','.psm','.pad','.log','.txt') })
Compress-Archive -LiteralPath @($files | Select-Object -ExpandProperty FullName) -DestinationPath $archive -Force
$manifest = @($files | ForEach-Object {
    @{name=$_.Name; size=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
})
ConvertTo-Json -InputObject @{stage=$Stage; returncode=$exitCode; timed_out=$timedOut; files=$manifest; archive_sha256=(Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()} -Depth 5 -Compress
