# DFlash2 KVMem context integration

This integrates the DFlash2 context fixes from
[melis2023/ninfer-kvmem-actkv](https://github.com/melis2023/ninfer-kvmem-actkv/commit/68fa0e0d67f8d4caafe90bc68bf79042c6b62271)
into the pinned NInfer backend. The implementation is adapted from the contributor's exported
working backend patch. Attribution: melis2023 <zhuangisme@gmail.com>.

The integration separates logical DFlash2 ring positions from compact target KV positions,
captures local attention with the logical capacity, masks stale conditioning below each row's
continuous recent suffix, restores the conditioning frontier, and prepares the full verification
width before a KVMem decode round. The optional attention floor is carried through the public Op,
its launch interface and both direct and split/reduce CUDA routes.

Current main already copies the draft ring with StateImage forks, including prefill checkpoint
forks. Its existing ownership and transfer paths remain authoritative; the contributor's duplicate
physical-slot tracking is unnecessary here. Append-only context work retains its paired logical
start/end bounds. The diagnostic-only bypass state is omitted because it never changes execution.

The backend base commit remains pinned. Changes are recorded in `backends/patches/ninfer-kvmem.patch`
and the patch digest and prepared tree in `backends/versions.json`. Other changes from the source
repository, including launchers, retrieval tuning, unrelated backend changes and README rewrites,
are outside this integration.

## Validation scope

`ninfer_dflash2_tests` groups the independent FP64 attention oracle, StateImage transfer tests
and StateImage layout checks. The attention tests cover both 2048/4096 rings, positions beyond the
ring capacity, null and per-row floors, empty context and inactive columns, and changing device
floors while replaying the same CUDA Graph. The server build checks all integration call sites.

The initial kernel/state validation uses the RTX 5050 Laptop
(`GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`), CUDA 13.2 and MSVC 19.44.

## GGUF companion and serving entry points

The follow-up imports the contributor's `dflash2_gguf` adapter and its recipe/source hooks.
The separate draft GGUF supplies its own geometry, conditioning taps and tensor aliases;
compatible feature, gate/up and selector projections retain their ggml blocks. Other projections
use the runtime's Q8 or BF16 formats. The serving parser and Engine validation now admit KVMem
with DFlash2 for one sequence on one GPU, resident/CPU Vision and 1 to 15 drafts. Other unsupported
combinations remain rejected. Parameter-boundary and exact GGUF block-import tests cover these
entry points.

The model used below is `incoai/Qwen3.8-27B-DFlash2-GGUF` at revision
`51962825493a48b846b40126d35c799ac4093ad0`, file
`Qwen3.8-27B-DFlash2-Q4_K_M.gguf` (1,143,006,816 bytes), downloaded through
[hf-mirror](https://hf-mirror.com/incoai/Qwen3.8-27B-DFlash2-GGUF/tree/main).
SHA-256: `1a25c56858e1ebe93f2718ac1d49d1151f9323325c1bbfd6209370f4db131ebd`.

## Local Task 1 qualification, 2026-10-18

The RTX 5060 Ti (`GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`) runs the existing
Qwen3.8-27B GSQ-RCO IQ3_S target, Vision and MTP objects unchanged. The imported mixed-format
draft occupies 1.73 GB on disk. Each variant runs two measured long-text/image/code conversations
after a full warmup per process. MTP3 and DFlash2-3 run in ABBA order; the documented DFlash2-7
setting is measured afterward. Requests use the same user turns, seed 42, temperature 1,
top-p 0.95/top-k 20, thinking budget 128 and output cap 512; sampled histories differ by variant.
All six measured image responses identify the red square, blue circle and green triangle.

Common settings: one lane, logical context 262144, INT8 KV, prefill 512, resident Vision with
1024 merged tokens, FP16 GDN state, LRFU and no n-gram drafting. The original 36864/16384
history/reserve setting exceeds available runtime memory with DFlash2 by about 0.8 GB. Both
variants therefore use 16384/4096, which contains this approximately 13K-token workload.

| Measured stage | MTP3, tok/s | DFlash2-3, tok/s | DFlash2-7, tok/s |
|---|---:|---:|---:|
| Long-text response | 54.69 | 63.51 | 60.24 |
| Image response | 63.23 | 60.78 | 58.21 |
| Code with thinking, 512-token cap | 72.50 | 75.24 | 73.09 |

Rates use aggregate `(output_tokens - 1) / decode_seconds`. DFlash2-3 improves the code rate
by 3.8% while the image response rate regresses by 3.9%; DFlash2-7 regresses image response
rate by 7.9% and does not improve on DFlash2-3. All complete conversations take about 29.2 seconds,
dominated by roughly 17.3 seconds to the first long-text response token. Peak dedicated VRAM is
13327 MiB for MTP3 and 14669/14679 MiB for DFlash2-3/7, approximately 1.31 GiB more.

Two repeats establish this Task 1 observation, not a general speedup. Different sampled output
lengths affect short-answer and whole-conversation comparisons. The capped code output does not
establish complete HTML correctness. Long-context selection/eviction, 256K recall and the
contributor's wider performance claims remain unverified.
