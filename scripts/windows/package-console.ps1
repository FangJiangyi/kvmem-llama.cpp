#Requires -Version 5.1
# Add the offline, one-click console to a worker package (or a new console-only folder).
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$OutputDir,
    [Parameter(Mandatory)][string]$PythonArchive,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$PythonSha256,
    [string]$SourceDir
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Get-FileHash is a module function in Windows PowerShell. Load this host's own
# module when invoked from Python or PowerShell 7 with an inherited module path.
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1') -Force
if (!$SourceDir) { $SourceDir = Join-Path $PSScriptRoot '../..' }
$source = (Resolve-Path -LiteralPath $SourceDir).Path
$archive = (Resolve-Path -LiteralPath $PythonArchive).Path
$output = [IO.Path]::GetFullPath($OutputDir)
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine $PythonSha256) {
    throw 'Python archive SHA-256 does not match the release publisher supplied checksum'
}
$files = @('START-HERE-CN.bat', 'START-HERE-EN.bat', 'scripts/local-console.py', 'scripts/windows/start-ui.ps1',
           'scripts/windows/describe-launch-json.ps1', 'scripts/windows/start-backend.ps1',
           'scripts/windows/start-ninfer.ps1', 'scripts/windows/start-dflash2.ps1',
           'scripts/windows/native-process.ps1', 'scripts/windows/select-model.ps1')
foreach ($name in 'index.html', 'monitor.css', 'monitor.js', 'i18n.js', 'launcher.css', 'launcher.js',
                  'Strata-LICENSE.txt', 'Outfit-OFL.txt', 'outfit-latin-wght.woff2') {
    $files += 'ui/monitor/' + $name
}
# Validate all inputs and reserved destinations before adding anything to a worker package.
foreach ($relative in $files) {
    if (!(Test-Path -LiteralPath (Join-Path $source $relative) -PathType Leaf)) { throw "Missing source: $relative" }
    $destination = Join-Path $output $relative
    if (Test-Path -LiteralPath $destination) {
        if (!(Test-Path -LiteralPath $destination -PathType Leaf) -or
            (Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath (Join-Path $source $relative)).Hash) {
            throw "Refusing to replace an existing package file: $relative"
        }
    }
}
foreach ($relative in 'runtime/python', 'console-build.json', 'START-HERE.txt', 'licenses/Python-LICENSE.txt') {
    if (Test-Path -LiteralPath (Join-Path $output $relative)) { throw "Destination already exists: $relative" }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    $names = @($zip.Entries | ForEach-Object FullName)
    if ('python.exe' -notin $names -or 'LICENSE.txt' -notin $names -or
        !@($names | Where-Object { $_ -match '^python\d+\.zip$' }).Count -or
        !@($names | Where-Object { $_ -match '^python\d+\._pth$' }).Count) {
        throw 'Supply the official Windows x64 embeddable Python ZIP, not an installer or virtual environment'
    }
    foreach ($entry in $zip.Entries) {
        # The official archive is flat. Reject links/traversal and unexpected nested payloads.
        if ($entry.FullName -match '[/\\:]' -or $entry.FullName -in @('.', '..')) {
            throw 'Unexpected path in the embeddable Python archive'
        }
    }
} finally { $zip.Dispose() }
$runtime = Join-Path $output 'runtime/python'
$null = New-Item -ItemType Directory -Path (Split-Path $runtime) -Force
[IO.Compression.ZipFile]::ExtractToDirectory($archive, $runtime)
$python = Join-Path $runtime 'python.exe'
$version = @(& $python -I -S -c 'import sys,ctypes,json,http.server,urllib.request,webbrowser; assert sys.version_info >= (3,9); assert sys.maxsize > 2**32; print(sys.version.split()[0])')
if ($LASTEXITCODE -ne 0) { throw 'Bundled Python failed its x64 standard-library check' }
foreach ($relative in $files) {
    $destination = Join-Path $output $relative
    $null = New-Item -ItemType Directory -Path (Split-Path $destination) -Force
    if (!(Test-Path -LiteralPath $destination)) { Copy-Item -LiteralPath (Join-Path $source $relative) -Destination $destination }
}
$null = New-Item -ItemType Directory -Path (Join-Path $output 'licenses') -Force
Copy-Item -LiteralPath (Join-Path $runtime 'LICENSE.txt') -Destination (Join-Path $output 'licenses/Python-LICENSE.txt')
$readme = @'
KVMem - start here

Double-click START-HERE-EN.bat for English startup instructions and an English UI.
START-HERE-CN.bat keeps the original Chinese startup instructions.
Both entries open the same local console and reuse its running services.
Python is bundled; no Python, pip, Node.js or build-tool installation is needed.
Keep the console window open. Closing it stops services started by this console.
The window prints shutdown instructions in the selected startup language.
Ctrl+C also closes it. Stop service in the UI stops inference while keeping the console
available; closing only the browser leaves services running. Separately started backends
are unaffected.
Repeated clicks reopen this installation's running console without restarting its model.

In Launch configuration, choose a backend, a local model and a GPU. Check configuration,
then Start service. Open Chat when the service is ready. Edits do not change a running model.
Models and inference workers must be included separately; the console does not download them.
Put ninfer-kvmem-server.exe, ninfer-serve.exe or llama-kvmem-server.exe beside START-HERE-CN.bat,
or in bin or workers/<backend>, with their runtime dependencies and chat UI.
Leave Backend executable empty for detection. Manual paths and worker-paths.json take
precedence. Detection does not execute files or check binary compatibility.
If no default worker is found, choose its executable in Engine-specific settings.
Use Find models to scan nearby model libraries, or choose/paste a model folder.
Explicitly scanned folders are remembered locally, including across console port changes.
The dialog shows search locations, incomplete scans, other formats and empty files.
Filter results by name or path, then select a model. This does not start inference.
If the Windows folder picker is unavailable, paste a full folder path instead.
Both engines use the same GPU cards. Switching engines preserves common resource,
cache and service settings; model assets, KV formats and native options remain separate.
This launcher currently exposes single-GPU llama.cpp and multi-GPU NInfer configuration.
A multi-GPU selection is preserved on switch to llama.cpp, with guidance to choose one
GPU before launching. The launcher does not silently choose a different card.
For llama.cpp, Cache & history provides independent K and V cache format selectors.
Quantized q8_0/q5_0/q4_0 types can be combined, as can f16/f32 types. Float/quantized
mixing is rejected. Existing combined KV selections initialize both selectors.
After updating files over a running console, close its startup window, restart it,
then refresh the browser to use the updated controller settings.
Enable MTP under Hardware & acceleration to use speculative decoding on supported
backends/models. Turning it off retains draft length, adaptive MTP and N-gram choices
while keeping them inactive. Re-enabling restores them; an initial draft length is 4.
Use an English path for the installation and models. Install the driver and VC++ runtime
required by the selected worker. Read the worker's validation report for supported GPUs.

Default console: http://127.0.0.1:18199/monitor/
Default inference service: http://127.0.0.1:18200/
For another console port: START-HERE-CN.bat -Port 18198
English entry with another console port: START-HERE-EN.bat -Port 18198
If the port is unavailable, the startup window asks for another port. Enter a number
from 1 to 65535, or press Enter/type q to cancel. The browser opens the selected address.
This only changes the console port; the inference port remains a separate UI setting.
For a diagnostic check: START-HERE-CN.bat -Check
To skip opening a browser: START-HERE-CN.bat -NoBrowser

The console controls only services it starts. It never replaces a running worker.
API keys are not stored in drafts or templates. Browser settings remain browser-local.
'@
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $output 'START-HERE.txt'), $readme, $utf8)
$hashes = @{}
foreach ($relative in $files) { $hashes[$relative] = (Get-FileHash -LiteralPath (Join-Path $output $relative)).Hash.ToLowerInvariant() }
$manifest = @{ schema_version=1; python_version=[string]$version[-1];
    python_archive_sha256=$PythonSha256.ToLowerInvariant(); offline_console=$true;
    models_included=$false; controller_files=$hashes }
[IO.File]::WriteAllText((Join-Path $output 'console-build.json'), ($manifest | ConvertTo-Json -Depth 5), $utf8)
Write-Output "One-click console added: $output"
