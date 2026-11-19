# csw installer for Windows: copies csw and its modules to the user's local prefix
# and adds a csw.cmd launcher. Optional: -WithProxy builds raine/claude-code-proxy.
param([switch]$WithProxy)
$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$binDir = Join-Path $HOME '.local\bin'
$dataDir = Join-Path $HOME '.local\share\csw'

# The py launcher avoids the Microsoft Store python.exe stub.
$python = $null
foreach ($candidate in @(@('py', '-3'), @('python'))) {
    if (Get-Command $candidate[0] -ErrorAction SilentlyContinue) {
        $exe = & $candidate[0] @($candidate[1..9] | Where-Object { $_ }) -c 'import sys, tomllib; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { $python = $exe.Trim(); break }
    }
}
if (-not $python) { throw 'csw needs Python >= 3.11 (tomllib). Install it from python.org or: winget install Python.Python.3.12' }

New-Item -ItemType Directory -Force $binDir, $dataDir | Out-Null
Copy-Item (Join-Path $here 'csw') (Join-Path $binDir 'csw') -Force
Copy-Item (Join-Path $here 'modules\*.py') $dataDir -Force
Copy-Item (Join-Path $here 'contrib\csw.ps1') $dataDir -Force
# cmd shim: works from cmd, PowerShell and Git Bash alike.
Set-Content (Join-Path $binDir 'csw.cmd') "@`"$python`" `"%~dp0csw`" %*" -Encoding ASCII
Write-Host "installed: $binDir\csw (+ csw.cmd, modules in $dataDir) using $python"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable('Path', "$binDir;$userPath", 'User')
    Write-Host "added $binDir to the user PATH (open a new terminal)"
}

if ($WithProxy) {
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw '-WithProxy needs cargo (https://rustup.rs)' }
    $src = Join-Path ([IO.Path]::GetTempPath()) 'claude-code-proxy'
    Remove-Item -Recurse -Force $src -ErrorAction SilentlyContinue
    git clone --depth 1 https://github.com/raine/claude-code-proxy.git $src
    if ($LASTEXITCODE) { throw 'git clone failed' }
    Push-Location $src
    try { cargo build --release; if ($LASTEXITCODE) { throw 'cargo build failed' } } finally { Pop-Location }
    Copy-Item (Join-Path $src 'target\release\claude-code-proxy.exe') $binDir -Force
    Remove-Item -Recurse -Force $src
    Write-Host 'installed: claude-code-proxy.exe (csw starts it on demand; no service needed)'
}

Write-Host ''
Write-Host 'next steps:'
Write-Host '  1. add a provider:      csw add glm|deepseek|orcarouter|claude <name>'
Write-Host '  2. list profiles:       csw list'
Write-Host '  3. switch profile:      csw <profile>     (or: csw run <profile> to keep others open)'
Write-Host "  4. optional wrapper:    add  . `"$dataDir\csw.ps1`"  to your `$PROFILE"
