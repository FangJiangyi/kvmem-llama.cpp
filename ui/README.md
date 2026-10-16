# Lightweight chat UI

This is a small Svelte entry point for the pinned llama.cpp UI. It reuses the upstream Markdown/code renderer (including sandboxed HTML preview), input components, SSE record helpers, and Dexie conversation storage. It does not initialize the upstream router/agent/MCP/stream-resumption lifecycle.

Build from the repository root with Node.js 22 and npm:

```bash
python3 scripts/build-webui.py
```

The script exports the pinned `llama.cpp/tools/ui` into ignored `build-ui/`, applies the local entry points there, checks types, and writes static files to `build/share/kvmem/ui/`. Source archives without Git metadata use their bundled UI source. The llama.cpp working tree is not modified. UI history/settings use a separate `kvmem-chat` browser namespace.

Rebuild the server, start the usual IQ3/IQ4 recipe, and open `http://127.0.0.1:18200/`. No Node.js is needed on machines running a package that includes the built page. `--ui-dir PATH` overrides the static directory and `--no-ui` disables it. `scripts/prebuilt/package.py --ui-dir PATH` can include separately built assets.

The settings panel leaves unmodified parameters out of requests. Template reasoning effort and thinking-token budget are separate. The output limit includes reasoning. Histories are browser-local; normal continuation sends the complete history without `cache_reset`. The page does not execute tools. Stop closes the request; reloading does not resume an interrupted stream.

The speed displayed is generated tokens divided by the server's generation duration, including reasoning tokens but excluding prefill and post-generation cache commits. Very short replies are not throughput benchmarks.

## Runtime monitor

Open `/monitor/` on the same server. The lightweight chat header opens it in a new tab,
so monitoring does not interrupt a streamed answer. The page follows the Strata layout,
defaults to 极客黑, and offers 架构蓝 and system themes.

`scripts/build-webui.py` includes `ui/monitor/` in both lightweight and full builds.
To update only monitoring assets in an existing built UI (no Node.js needed):

```bash
python scripts/build-webui.py --monitor-only --output build/share/kvmem/ui
```

llama.cpp serves these files through `--ui-dir`. The lightweight chat entry is
specific to llama.cpp; for NInfer, build `--full-ui` into a separate output directory,
configure `-DNINFER_WEBUI_DIR=/absolute/path/to/that/output` and build `ninfer-serve`;
the static files are embedded in its executable. Both backends must be rebuilt to
provide `/api/monitor/snapshot`. The endpoint follows the existing API-key gate and
uses `Cache-Control: no-store`; the static page itself is public. Enter the key in
the page when prompted. It is stored only in that tab's `sessionStorage`.

The monitor keeps up to 100 finished request metadata records in server memory,
plus up to 512 active records; excess records are counted explicitly. It stores no
prompt, reply, sampling parameters or key. Restarting the server resets the history.
Streaming request progress is sampled from accepted-token and prefill observations;
NInfer aggregate responses expose final token/timing statistics when complete.
NInfer history follows its generation lifecycle: authentication, protocol parsing,
and unknown-model failures before that lifecycle are not recorded as generation requests.
Live readings are not a benchmark. CPU/RAM telemetry supports Windows and Linux.
NVIDIA telemetry uses the optional driver NVML library when the CUDA SDK headers
are available at build time. Unsupported fields are null, never substituted with
demonstration values. Device identity follows inference placement; monitoring does not
change that placement. Cache disk throughput is
derived from KVMem's read-byte counters, not whole-system disk I/O.

The browser polls without overlapping calls, reduces polling in hidden tabs, and
marks the last readings stale on failures. Server restarts clear chart history.
Monitoring has its own short state mutex; hardware sampling runs on a separate
thread and does not acquire model execution locks.

Verification: `node ui/test-monitor.cjs` (Playwright, optionally `CHROMIUM` and
`PLAYWRIGHT_MODULE`), `ctest -R kvmem-monitor-test`, and
`python tests/test_webui_builder.py`. Static layout/font attribution is distributed
with the page as `Strata-LICENSE.txt` and `Outfit-OFL.txt`.

`scripts/test_monitor.py --binary PATH --model PATH --ui-dir PATH --gpu GPU-UUID
--log PATH [--backend ninfer]` starts and stops its own disposable server. It checks
static assets, API-key enforcement, polling during generation, usage agreement,
rejections, client disconnects, and telemetry on the selected NVIDIA device.

`/monitor/` shares the current service origin and API authentication. It shows request statistics,
then the inference GPUs bound to the loaded model: a wide single-device card or two side-by-side
cards (stacked on narrow screens). Other physical GPUs appear under About → Hardware. The model's
`/props.execution.monitor_devices` supplies CUDA UUIDs for NInfer and physical PCI identities for
llama.cpp; these match NVML hardware samples, never numeric NVML indices or instantaneous load.
Missing binding information is explicit and does not fall back to displaying an arbitrary card.

Service state follows active requests independently of the selected historical request. Decode,
prefill, logical context and reused input tokens are request metrics; the output meter shows token
budget use, not completion. GPU readings describe the whole card. RAM/SSD history capacity remains
visible, with session counts, history hits/evictions and cache disk reads in expandable details.
Themes are 极客黑 (default), 架构蓝, and system. Fonts and assets are bundled locally.
The header language picker switches between 中文 and English without reloading or
resetting request selection and chart history. Language preference is stored locally;
Chinese remains the default. Labels, statuses, errors and accessibility text are
translated locally; model/device identities and original license texts are preserved.
Navigation is ordered Launch configuration → Monitor → About, with Chat on the right.
The toolbar's **Layout: Comfortable / Compact** selection applies to monitoring and launch
configuration and persists per browser, independently of theme and language. Comfortable
is the default. Compact reduces spacing, controls, gauges and chart heights; long request,
template and log lists scroll internally. Common field help can be expanded, while backend
limitations and warnings remain visible. Changing density does not reload data, reset edits,
or restart inference. Desktop compact mode places the single-GPU gauges beside their trends.
Logical context, GPU utilization and whole-card VRAM use 270-degree gauges with
numeric readings. Decode speed and GPU utilization retain their time-series charts.
Missing readings show an empty arc and an unavailable label; zero has no colored arc.

`ui/test-monitor.cjs` supplies model-free browser checks; set `MONITOR_FIXTURE_ONLY=1` to serve its
fixture on localhost:18412 for manual browser checks. `MONITOR_FIXTURE_FILE` can point to a JSON
scenario using `dual`, `idle`, `missing`, `stale`, `unknown`, `cpu`, or `failure` switches.
`scripts/test_monitor.py` verifies disposable real-model services, including physical device
identity with CUDA-visible remapping. These fixtures never belong in the built monitor directory.

## Independent local console (Windows)

The desktop monitor now includes **Launch configuration**: six essential settings,
five common-setting tabs, and a collapsed engine-specific section. It uses the full
window width, with process state and lifecycle actions in a right-hand column.
Low-frequency history controls are collapsed. The monitor uses the same theme and
language controls. Small windows stack the columns; expanded details may scroll.

The right sidebar has one built-in template: **NInfer · 0.18.0 default**, matching
`scripts/windows/start-ninfer.ps1` after release update `fd77613` (2026-10-13).
It uses context 204800, GPU history 36864, reserve 16384, RAM history 12288 MiB,
prefill 256, output limit 16384, INT8 KV, one concurrent request and one retained session.
It enables adaptive MTP with 4 drafts, N-gram with 31 drafts/minimum match 12,
CPU-resident vision with 1024 tokens, and thinking budget 8192. Sink/recent are
12288/1024; LRFU uses lambda 0.5/top fraction 0.9 and max score aggregation.
The original `v0.18.0` Git tag predates the sink/recent release update.
The recipe requires a model with Vision/MTP support. Other built-in recipes were removed;
personal templates are unaffected. Model/worker/tokenizer paths are retained per backend,
as are the chosen port and GPU order; same-backend manual layer placement is retained.
Applying the template only edits and autosaves the draft, with Undo available. It never
starts or changes the running service. The template is disabled if NInfer is unavailable.

Start the independent host from the repository root:

```powershell
python scripts/local-console.py
```

Open `http://127.0.0.1:18199/monitor/`. This host serves the UI before a model is loaded
and remains available after inference stops. It requires Python and Windows PowerShell;
there is no new compiled-server dependency. Opening `/monitor/` directly on an inference
server continues to work, but its configuration tab explains how to open the local console.

The console uses `scripts/windows/start-backend.ps1 -DryRun` from its backend checkout
for validation and argument translation. It then starts that exact argument vector,
without a shell. A separate mainline checkout or unpacked worker installation can be selected:

```powershell
python scripts/local-console.py --backend-root C:\path\to\backend-checkout
```

The model field has a **Choose model** button that opens the local Windows file dialog,
filtered to `.ninfer` or `.gguf` for the selected backend. It fills the absolute path;
the model is never uploaded or copied through the browser. Cancellation keeps the old
selection. The eight most recent picker selections per backend are stored in this
browser's local storage and offered in a dropdown. Manual file-path input remains
available. The dialog opens on the controller's desktop, with one picker at a time;
the selection is automatically saved as an edit and never starts inference. The authenticated local mutation endpoint
validates the selected file and extension, and closes a picker left open for five minutes.

**Quick scan for models** lists local candidate filenames, sizes and paths for the
selected backend (`.ninfer`, not `.infer`; otherwise `.gguf`). It searches conventional
`models` folders under the UI/backend checkout and their two nearest parent directories,
plus the current model's folder. A folder picker can select another local search root.
It never loads a model, uploads data, or starts a service. Scans are bounded to four
subdirectory levels, 10,000 directory entries, 200 results and approximately three seconds
(individual filesystem calls can still block). Partial or inaccessible results are marked.
Drive roots/network shares, child symlinks/junctions, hidden dependency folders, named
`mmproj`/projector files and non-first GGUF shards are excluded. Filename matching is only
candidate discovery, not architecture/quantization compatibility validation.

This console supports NInfer and llama.cpp. Only these two backends can be selected,
even when another checkout's launcher declares experimental engines. Unsupported sampling/template controls
are explicitly unavailable. Native options keep their original spellings, with a small
allowlist of numeric thread/GPU-offload options; arbitrary shell text and duplicate common
options are rejected. Multi-GPU selection is currently exposed for NInfer layer splitting.

The adapter checkout can differ from the executable installation. Register absolute
executable paths by backend in `<state-dir>/worker-paths.json`, for example
`{"ninfer":"C:\\installed\\ninfer-serve.exe","llamacpp":"C:\\installed\\llama-kvmem-server.exe"}`.
An empty Worker field uses this registration, or the selected checkout's packaged
`workers/<backend>/...` default when no registration exists. The UI shows the detected
path. An explicit Worker always takes precedence; a missing explicit or registered
file fails clearly, without silently choosing another build.

Edits are automatically saved in this browser, including incomplete inputs and GPU order.
The heading shows saving/saved status and offers retry on storage failure. Reloading restores
the browser draft; clearing site data removes it. The server-side v1 draft remains the
initial fallback and is available through the existing save API.
`Save as template` stores a named snapshot with an optional description under **My templates**.
Personal templates include machine-specific paths and all settings, and support apply/undo,
rename, explicit update from the current draft, and confirmed deletion. They are stored in
this browser alongside edits, not shared across browsers. Built-in recipes remain separate.
`Check configuration` opens a dialog with model, GPU order/layers, cache budgets, resolved
executable and port availability. Errors link back to relevant fields where identifiable.
The canonical PowerShell command is collapsed by default and can be copied. Checking does
not load a model or prove model compatibility/resource fit; it never starts inference.
`Start service` validates again, rejects an occupied port or a second managed process,
and displays loading/ready/exited states and the log tail. `Stop service` terminates only
the owned Windows Job process tree, unloading the model and its in-memory history. The
job also closes on controller exit. A gated child is assigned to the job before spawning
the worker, so wrappers cannot create unowned descendants. No existing service is adopted
or stopped, and there is no implicit restart. Draft and active configuration are separate.
The service card shows an indeterminate progress bar while the launch request is pending
and while waiting for backend health readiness. It shows completion only after the health
check succeeds; failures/exits stop the animation and expose the error or log guidance.
No loading percentage is inferred. The Chat link remains in the top-right toolbar and
is disabled until the managed service is ready, so availability changes do not move navigation.
The console proxies only four read-only monitoring endpoints; chat opens on the worker's
own origin. The controller remains loopback-only, validates Host/Origin/fetch-site, requires
a per-run token on mutations, and serves only an explicit static-file allowlist.
The inference service's Listen address can be `127.0.0.1` (this computer, default) or
`0.0.0.0` (all IPv4 interfaces). With all interfaces selected, other devices use this
computer's IP address and the configured port. Preview and launch check availability on
the selected listen address. Health checks, monitor proxying and the local Chat link
continue to use `127.0.0.1`; the console itself still listens only on loopback.

The persisted v1 console format is `{schema_version: 1, parameters: {...}}`, using typed
launcher parameter names. It is a bridge to the existing launcher, not an implementation
of the separate proposed `common/backends` public configuration schema. Unexposed settings
are omitted so native defaults survive; unsupported keys/types fail rather than being
silently ignored. Raw API keys are not a console configuration option.

Checks: `python tests/test_local_console.py`, `python tests/test_webui_builder.py`,
`node ui/test-monitor.cjs`, and `node ui/test-launcher.cjs`. The launcher browser check
uses a disposable controller and state directory. Optional `TEST_MODEL`, `TEST_WORKER`,
and `TEST_GPU` enable a real llama.cpp start/generate/monitor/stop check. `BACKEND_ROOT`
selects its adapter checkout, and `CONSOLE_TEST_PORT` defaults to 18431 (worker uses +1).
`CHROMIUM` and `PLAYWRIGHT_MODULE` select the locally installed browser/runtime.

## Chat regression checks

Run against a disposable test service, not an active conversation. The HTTP check needs a model that can answer simple arithmetic, with a generation reserve of at least 128. The browser check uses up to 512 generated tokens and requires Playwright's Chromium or `CHROMIUM` pointing to a compatible installation.

```bash
KVMEM_UI_TEST_URL=http://127.0.0.1:18400 python3 scripts/test_webui.py
KVMEM_UI_TEST_URL=http://127.0.0.1:18400 node ui/test-browser.mjs
python3 scripts/test_start_server.py
```

Browser tests check real generation, local history after reload, code rendering, stop/continuation, and the absence of unsupported backend requests. Local image upload/follow-up was also checked on IQ3 27B with GPU Q8 vision and MTP3/ReplaySSM.
