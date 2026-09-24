#requires -Version 5.1
<#
.SYNOPSIS
Keep a localhost dashboard SSH tunnel alive and retry transport failures.
.EXAMPLE
./scripts/dashboard-tunnel.ps1 -SshHost user@server -LocalPort 9876
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|\[?[0-9a-fA-F:]+\]?)$')]
    [string]$SshHost,
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 18765,
    [ValidateRange(1, 65535)]
    [Nullable[int]]$SshPort = $null,
    [ValidateRange(1, 60)]
    [int]$RetrySeconds = 5
)

$ErrorActionPreference = 'Stop'
# PowerShell 7 may otherwise throw before SSH's exit status can be classified.
$PSNativeCommandUseErrorActionPreference = $false
$sshCommand = Get-Command ssh -CommandType Application -ErrorAction Stop | Select-Object -First 1
$delay = $RetrySeconds

Write-Host "Dashboard: http://127.0.0.1:$LocalPort/ -> server 127.0.0.1:9876"
Write-Host 'Keep this window open. Ctrl+C stops the tunnel and retries.'
Write-Host 'Authentication stays with OpenSSH; password login may prompt again on reconnect.'

while ($true) {
    # Check without connecting to, closing or replacing another tunnel.
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $LocalPort)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
    }
    catch {
        throw "Cannot bind 127.0.0.1:$LocalPort. Close the old tunnel yourself, or choose another -LocalPort. No existing process was stopped."
    }
    finally {
        $listener.Stop()
    }

    $logPath = [System.IO.Path]::GetTempFileName()
    try {
        $sshArguments = @('-N', '-T', '-E', $logPath,
            '-o', 'ForkAfterAuthentication=no', '-o', 'ControlPath=none', '-o', 'LogLevel=ERROR',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=6',
            '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1',
            '-L', "127.0.0.1:${LocalPort}:127.0.0.1:9876")
        if ($null -ne $SshPort) { $sshArguments += @('-p', [string]$SshPort) }
        $sshArguments += $SshHost
        Write-Host 'Connecting with OpenSSH (15-second keepalive; 6 missed replies allowed)...'
        $started = [System.Diagnostics.Stopwatch]::StartNew()
        # Keep stdin and console handles intact for password/host-key prompts.
        & $sshCommand.Source @sshArguments
        $sshExit = $LASTEXITCODE
        $started.Stop()
        $diagnostic = [System.IO.File]::ReadAllText($logPath)
    }
    finally {
        Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
    }

    if ($sshExit -eq 0) {
        Write-Host 'SSH exited normally; no reconnect requested.'
        break
    }
    if ($diagnostic.Trim()) { Write-Host $diagnostic.Trim() }
    # A failed backend HTTP forwarding channel does not mean the SSH transport
    # failed. Do not let an earlier channel error classify a later unknown exit.
    $terminal = (($diagnostic -split '\r?\n') | Where-Object { $_ -notmatch '^\s*channel\s+\d+:' }) -join "`n"
    $permanent = 'Permission denied|Authentication failed|Too many authentication failures|Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|Bad configuration|Bad .*forwarding|Could not resolve hostname|Name or service not known|Address already in use|cannot listen to port|Could not request local forwarding|administratively prohibited'
    $transient = 'Connection reset|Connection timed out|Operation timed out|Connection closed|closed by remote host|Broken pipe|Connection refused|No route to host|Network is unreachable|Timeout, server .* not responding|Control master terminated unexpectedly'
    if ($terminal -match $permanent -or $sshExit -ne 255 -or $terminal -notmatch $transient) {
        throw "SSH stopped (exit $sshExit); automatic retry disabled for authentication, host-key, configuration, bind or unknown errors. Fix the message above and run again."
    }
    if ($started.Elapsed.TotalSeconds -ge 120) { $delay = $RetrySeconds }
    Write-Host "SSH transport interrupted. Reconnecting in $delay seconds; Ctrl+C cancels."
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min(60, $delay * 2)
}
