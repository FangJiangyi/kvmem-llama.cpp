# Mainline desktop console validation - 2026-10-16

The console and runtime monitor integrate with the mainline NInfer and llama.cpp
launchers. Experimental Strata/PolyStrata adapters are excluded from the UI and
controller allowlist. No experimental engine dependency or submodule revision is added.

## Delivered behavior

- Independent Windows Python console, available before model startup and after shutdown.
- Model picker, bounded model discovery, registered or explicitly selected worker paths.
- Browser-local autosaved edits and named templates, apply/undo and template management.
- A single built-in NInfer 0.18.0 recipe including the sink 12K/recent 1K release update.
- Canonical PowerShell validation, structured preview and occupancy checks for the selected
  loopback/all-interface listen address. Health/proxy/chat connections use loopback.
- Owned Windows Job lifecycle, startup waiting animation and readiness/error feedback.
- Monitor snapshots from both backends, bounded request metadata and hardware sampling.
- Chinese/English, dark/blue themes, comfortable/compact density and aligned form rows.
- Existing CLI option names remain unchanged; positive UI switches map to native options.

## Verification for mainline integration

- 15 controller tests: mainline backend restriction, draft persistence, worker resolution,
  native argument validation, literal paths, GPU order/layers, listen address and port checks,
  model picker/discovery, and HTTP token/origin/static scope.
- Four UI packaging tests; monitor assets and original license files are included.
- Monitor browser checks: themes/languages/density, authentic/missing fixture values,
  GPU assignment, responsive geometry, CSS isolation, failure/reconnect/restart, key isolation
  and escaped content. The dual-GPU display uses fixtures.
- Launcher browser checks against this checkout's mainline launcher, with no external
  backend-root override. Covers presets, autosave, incomplete edits, templates, ordered GPUs,
  LRFU switch semantics, listen selection, progress/readiness/failure and responsive rows.
- Real llama.cpp browser lifecycle: Qwen3.5-0.8B-Q8_0 on RTX 5050 Laptop
  (`GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`), canonical preview, Start, ready,
  generation, monitor proxy, duplicate-start rejection, Stop and worker-port closure.
- MSVC 14.44/CUDA Release build checks for llama-kvmem-server and ninfer-serve, 8 jobs.
  NInfer native sources were already built; its build check required no source recompilation.
- C++ monitor, server options and server progress tests passed.
- Real llama.cpp monitor smoke: public assets, API-key gate, generation-time polling,
  usage agreement, rejected requests, disconnected streams and physical GPU identity.
- Both prepared backend trees match their versioned patches and versions.json.
- Python controller/packaging tests are registered in Windows and Linux host CI.

## Scope and limits

Launch/process control and the native picker require Windows and Python. Monitoring
works through monitor-enabled workers; installing this source change does not upgrade
previously published binaries. Models and runtime workers are supplied separately.
The existing advanced llama.cpp launchers remain available for options not exposed here.

This integration reran the full real-model lifecycle for llama.cpp. NInfer's real-model
monitoring was verified earlier in this development checkout; this pass checks its build
and canonical parameter bridge without interrupting the active RTX 5060 Ti service.
No new throughput measurement or real dual-GPU model run is claimed.
