# Windows one-click console

Extract the complete package to an English path. Double-click `START-HERE-EN.bat` for
English startup instructions and an English UI, or `START-HERE-CN.bat` for the original
Chinese startup instructions. Both entries share the same console and running services.
The launcher opens `http://127.0.0.1:18199/monitor/`. Choose a backend, local model and GPU,
check the configuration, then start the service. The Chat link becomes available when the
worker is ready. The controller window stays open; closing it stops its owned services.
Repeated clicks reopen the same installation's console without restarting its model.
After a new controller starts, the window prints instructions in its startup language for closing it or
pressing Ctrl+C, stopping inference from the UI while keeping the console open, and closing
only the browser. The instructions explain that separately started backends are unaffected.
Reopening an existing controller does not print instructions implying the second window owns it.

An offline console package includes the official Windows x64 embeddable Python runtime in
`runtime/python`. No Python installation, pip packages, Node.js or build tools are needed.
A source checkout can use installed 64-bit Python 3.9+ instead. The entry does not download
or install anything, alter PATH, request administrator access, or load a model automatically.
Model/worker selection, browser drafts, personal templates and API key behavior follow the
[console documentation](../ui/README.md#independent-local-console-windows).

Models and inference workers are supplied separately. Workers still require their GPU driver
and Microsoft VC++ runtime, and their own model/architecture compatibility checks. The
previously published ZIPs do not gain this entry or the new monitor API automatically.

Place the console package's contents in the backend EXE directory to enable automatic
discovery. Keep both `START-HERE-CN.bat` and `START-HERE-EN.bat`, `runtime/`, `scripts/` and `ui/` together, and preserve the
worker's DLLs and chat resources. Leave **Backend executable** empty to use detection.
Recognized filenames are `ninfer-kvmem-server.exe`, `ninfer-serve.exe`, and
`llama-kvmem-server.exe`. The console also checks `bin/` and `workers/<backend>/`.
It checks the selected backend root first, then its own installation when different;
within each root the order is root, bin, workers, with the canonical NInfer name preferred
within a directory. Manual paths override registrations, which override discovery.
Missing manual/registered paths remain errors. Discovery does not load a model or certify
the worker's version/dependencies. It does not search other build trees recursively.

## Finding models

Use **Choose model** for a file, or **Find models** to browse/paste a model folder and inspect
its candidates. The native selection helper is included in the package. A failed system
dialog produces readable guidance; pasting a full folder path works independently of it.
Scanned folders are remembered on this computer across console ports. Forget folder only
removes the directory record. Search results show names, sizes, full paths, format hints and
empty-file warnings; selection fills the draft without starting inference. The scope and
partial-result messages explain where the scan looked and whether a limit was reached.
See [the model-finder contract and bounds](../ui/README.md#independent-local-console-windows).

## Options and diagnostics

```powershell
.\START-HERE-CN.bat -Port 18198
.\START-HERE-CN.bat -Check
.\START-HERE-CN.bat -NoBrowser
.\START-HERE-EN.bat -Port 18198
```

`-Check` validates the console files and Python standard library, without opening the browser
or checking model/GPU fit. Runtime state and logs default to `build/local-console` beneath
the installation; `-StateDir D:\KVMem-state` changes this location. The directory must be
writable. Browser drafts/templates belong to the console origin, so changing its port
uses a different browser store. The inference port remains a separate UI setting.
The English entry opens the UI in English even if the browser previously used Chinese.
The language picker remains available, and subsequent reloads remember a new selection.
Manual entries can use `start-ui.ps1 -Language en` or `local-console.py --language en`.

A conflicting console port prints a clear message and asks for another port in the startup
window. The default-port message is “默认端口 18199 不可用”. Enter a number from 1 to 65535;
invalid values are retried, and another unavailable port prompts again. Press Enter or type
`q` to cancel. The browser opens the successfully selected address. This only changes the
console port, not the inference port. Other applications keep running. The manual Python
entry enables this behavior with `--prompt-port`; without that flag it still exits on a conflict.
An existing console is reused only when its protocol, installation, backend root and state
directory all match. The error window stays open so a double-click failure can be read.

## Preparing a release

Download an official x64 **embeddable ZIP** from the [Python release page](https://www.python.org/downloads/windows/).
Verify its SHA-256 against that release's published value. The packaging script requires the
publisher's expected checksum, verifies it before extraction, and tests the embedded runtime.
It retains Python's original license and writes `console-build.json` with runtime version,
archive checksum and controller-file checksums.

To add the console to a worker package, or create a console-only folder:

```powershell
.\scripts\windows\package-console.ps1 `
    -OutputDir 'D:\packages\KVMem' `
    -PythonArchive 'D:\downloads\python-X.Y.Z-embed-amd64.zip' `
    -PythonSha256 '<official release SHA-256>'
```

The script copies the root entry, controller, canonical launch bridge and local monitor
assets. It refuses to replace differing files or an existing runtime. Use a fresh staging
directory on rebuild. Console-only output does not contain inference engines or models.

The combined worker packager also accepts `-PythonArchive`, `-PythonSha256` and
`-LlamaUiDir` (the built llama.cpp chat directory). These add the offline console before
the package's final checksums are generated. llama.cpp chat files are placed under
`workers/share/kvmem/ui`, matching the worker's binary-relative lookup; NInfer must be
built with its chat and monitor assets embedded. Keep monitor-enabled workers and console
assets on the same source revision.

Default executable locations:

- `workers/ninfer/ninfer-kvmem-server.exe`
- `workers/llamacpp/llama-kvmem-server.exe`

Supply each worker's DLL dependencies too. A separately installed worker may instead be
selected in the console's engine-specific settings. Shipping the console does not certify
those binaries, their model compatibility, or their performance. Run the complete final ZIP
through start/chat/monitor/stop acceptance before publishing.

Model-free verification:

```powershell
python tests/test_local_console.py
python tests/test_one_click.py
$env:KVMEM_TEST_PYTHON_ARCHIVE='D:\downloads\python-X.Y.Z-embed-amd64.zip'
python tests/test_one_click.py
```

The optional test checks checksum rejection, the actual batch entry in a path containing
spaces, a portable controller with system Python removed from PATH, local assets, and the
embedded worker gate. It starts only disposable controllers and does not load a model.
