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
contributor's wider performance claims remain unverified by Task 1.

### Five-draft follow-up

Two additional fresh processes each run one warmup and one measured Task 1 conversation at
five drafts, with the same model, sampler and settings above. The measured long-text, image and
capped-code rates are **73.09, 81.63 and 83.07 tok/s**. Relative to MTP3 these are +33.7%, +29.1%
and +14.6%; the capped-code rate improves 10.4% over DFlash2-3. Both image answers identify all
three shapes. Median conversation time is 27.86 seconds against MTP3's 29.32, with different
sampled response lengths. Peak VRAM is 14673 MiB. Width five follows the three/seven campaign;
this is not a fully interleaved width sweep.

[Task 1 measurements](benchmarks/dflash2-task1-20261018.json) retain the per-stage samples,
settings, model provenance and binary hashes. The five-draft binary differs from the earlier
measurements only by a CLI help-text rebuild; inference arithmetic is unchanged.

## Selected 16 GB startup profile

The source and Windows worker packages provide `scripts/windows/start-dflash2.ps1`:

```powershell
.\scripts\windows\start-dflash2.ps1 -Model 'D:/models/iq3-dflash2.ninfer' `
  -Gpu 'GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c' -DryRun
```

Omit `-DryRun` to start; source builds also supply `-Worker` for their `ninfer-serve.exe`.
The artifact must contain the IQ3 target and imported DFlash2 companion. The profile selects
logical context **262144**, history budget **36864**, generation reserve **10240**, five fixed
DFlash2 drafts, no n-gram, INT8 KV, FP16 GDN, prefill 256, CPU Vision capped at 1024 merged tokens,
one lane/session, 12 GiB host payload, LRFU lambda 0.5/top fraction 0.9/max aggregation,
sink/recent 12288/1024, no SSD and a **64 MiB CUDA Graph planning allowance**. Startup output and
thinking defaults remain 16384/8192. `-Context` can override the logical context.

On the RTX 5060 Ti the short text and CPU image probes pass; graph capture actually consumes
30 MiB. CPU image first-token latency is about **50.3 seconds**. This profile therefore does not
retain Task 1's resident-GPU Vision latency. FP16 reduces the model's recurrent state by 72 MiB
per state slot; increasing total physical KV from 44K to 46K adds 66 MiB, leaving the dedicated
VRAM peak only about 6 MiB lower. Lowering the graph allowance from 128 to 64 MiB alone changes
the admission budget, not the actual captured allocation. CUDA reports 198 MiB free after startup,
which must not be confused with the larger difference between NVML usage and the card's capacity.
These are launch-specific settings; the general `start-ninfer.ps1` defaults are unchanged.

## Task 2 at 256K, 2026-10-18

Both DFlash2 profiles replay the **same 33 saved RC3 Task 2 request payloads**, apart from the
model alias and conversation ID. The RC3 run is the existing 2026-10-13 calibrated cohort;
**RC3 was not rerun**. All three use the same RTX 5060 Ti UUID above, INT8 KV, no n-gram,
one lane/session, seed 42, temperature 1, top-p 0.95/top-k 20, output cap 512 and thinking budget
128. Both DFlash2 runs use the same executable SHA-256
`c21de70477dc8381a064c48c1d02535db4e3cfb034e5d29e642693d3142d43c3`, built from the PR #150
implementation (`1dd0a987a8c04fc206e876b926ebd50dc598c589` locally). Later backend merges were
not part of these measurements. No filler was removed from either DFlash2 replay.

| Measurement | RC3 MTP3 | DFlash2-5, 32K+12K / FP32 | DFlash2-5, 36K+10K / FP16 |
|---|---:|---:|---:|
| Completed requests / tool rounds with prefix hits | 33 / 32 | 33 / 32 | 33 / 32 |
| Final input tokens | 261522 | 261411 | 261411 |
| All-response output tokens | 15293 | 13785 | 15567 |
| Tool aggregate decode, historical formula, tok/s | 53.24 | 61.62 | 62.07 |
| Tool aggregate decode, interval formula, tok/s | 53.13 | 61.47 | 61.94 |
| Last eight tool rounds, historical formula, tok/s | 56.54 | 59.49 | 56.14 |
| Final response decode, tok/s | 44.87 | 84.32 | 67.52 |
| Final first-token latency, seconds | 16.87 | 17.27 | 16.80 |
| Sum of request wall time, seconds | 855.10 | 775.75 | 813.53 |
| Peak dedicated VRAM, MiB | 14456 | 15187 | 15181 |
| Minimum available system RAM, MiB | 8.78 | 344.21 | 286.31 |

The historical aggregate is `sum(output_tokens) / sum(decode_seconds)` over the 32 tool rounds;
the interval formula subtracts one output token per request for every cohort. The latest aggregate
improves 16.6% over RC3 and 0.7% over the earlier DFlash2 configuration, while the last-eight-round
aggregate regresses 0.7% against RC3 and 5.6% against the earlier DFlash2 configuration. Total time
improves 4.9% over RC3 but regresses 4.9% against the earlier DFlash2 run; the latest run generates
12.9% more output tokens than that earlier run. Each long-context profile has only one complete run.

This is a configuration comparison. RC3 uses 36K+16K, prefill 512, FP16 GDN, resident GPU Vision
and sink/recent 5120/5120, plus an older backend/frontend. The two DFlash2 profiles change B/R,
GDN precision and graph planning allowance together. Different outputs and severe system RAM
pressure affect wall time and decode rates; neither result isolates the effect of FP16 GDN.
The Task 2 fixture contains text/tool results, so it does not measure CPU image encoding.

**Operational completion is not completion of the requested code task.** All final responses hit
the 512-token cap. RC3 starts a Python utility before truncation; both DFlash2 final responses
remain in batch/checksum lists without reaching code. Some intermediate responses repeat prose.
The final decode-rate difference is therefore not a speedup for equivalent generated code. No
complete utility, 16K-token generation, or independent 256K recall/selection oracle was validated.

[Task 2 records](benchmarks/dflash2-task2-256k-20261018.json) include all 99 request timing/usage
rows, fixture hashes, binary/source provenance, per-context-band aggregates, settings and output
observations. The historical baseline is retained as recorded.
