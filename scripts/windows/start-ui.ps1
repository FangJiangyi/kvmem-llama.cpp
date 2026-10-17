#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 18199,
    [ValidateSet('zh-CN', 'en')][string]$Language = 'zh-CN',
    [string]$StateDir,
    [switch]$NoBrowser,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
try {
    foreach ($relative in 'scripts/local-console.py', 'scripts/windows/start-backend.ps1',
                          'scripts/windows/describe-launch-json.ps1', 'scripts/windows/native-process.ps1',
                          'scripts/windows/select-model.ps1',
                          'ui/monitor/index.html', 'ui/monitor/launcher.js', 'ui/monitor/monitor.js') {
        if (!(Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf)) {
            throw "Incomplete KVMem package: missing $relative. Extract the complete ZIP first."
        }
    }
    # Avoid Windows Store aliases and automatic installs by either Python launcher.
    $env:PYLAUNCHER_ALLOW_INSTALL = $null
    $env:PYLAUNCHER_ALWAYS_INSTALL = $null
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
    $probe = 'import sys,ctypes,json,http.server,urllib.request,webbrowser; assert sys.version_info >= (3,9); assert sys.maxsize > 2**32; print(sys.executable)'
    $embedded = Join-Path $root 'runtime/python/python.exe'
    $python = $null
    if (Test-Path -LiteralPath $embedded -PathType Leaf) {
        $output = & $embedded -I -S -c $probe 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'The bundled Python runtime is incomplete. Extract a fresh copy of the KVMem ZIP.' }
        $python = $embedded
    } else {
        foreach ($name in 'python.exe', 'py.exe') {
            $candidate = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (!$candidate -or $candidate.Source -like '*\Microsoft\WindowsApps\*') { continue }
            $arguments = @('-I', '-S', '-c', $probe)
            if ($name -eq 'py.exe') { $arguments = @('-3') + $arguments }
            try {
                $output = @(& $candidate.Source @arguments 2>$null)
                if ($LASTEXITCODE -eq 0 -and $output -and (Test-Path -LiteralPath ([string]$output[-1]) -PathType Leaf)) {
                    $python = [string]$output[-1]; break
                }
            } catch { continue }
        }
        if (!$python) {
            throw 'Python was not found. Use a KVMem package with runtime/python included, or install 64-bit Python 3.9+ from https://www.python.org/downloads/windows/ .'
        }
    }
    if ($Check) {
        Write-Output "KVMem UI files and Python runtime OK: $python"
        Write-Output 'This check does not load a model or validate GPU/worker dependencies.'
        exit 0
    }
    if (!$StateDir) { $StateDir = Join-Path $root 'build/local-console' }
    $arguments = @('-X', 'utf8', '-I', '-S', (Join-Path $root 'scripts/local-console.py'), '--port', "$Port", '--state-dir', $StateDir, '--prompt-port', '--language', $Language)
    if (!$NoBrowser) { $arguments += '--open-browser' }
    Write-Output 'KVMem - local launch console'
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
