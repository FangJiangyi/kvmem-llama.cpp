# RTX 5050 multi-client stress test, 2026-10-01

The two-lane server completed the load test without errors after fixing a
dormant decode-graph allocation bug. The run included four one-minute client
ramps, ten minutes of sustained cache churn, and recovery probes. It lasted
958.833 seconds including calibration, queued-request draining and cleanup.

## Configuration and workload

- Windows/MSVC, CUDA 13.2.86, NVIDIA driver 610.62.
- RTX 5050 Laptop GPU, 8 GiB; Qwen3.5-0.8B-Q8_0.gguf.
- `--parallel 2`, eight host stores, 24 logical conversation IDs during churn.
  At the time of this measurement, parallel was restricted to 1 or 2; client
  concurrency is independent. Subsequent four-lane measurements are recorded below.
- Context 8192, GPU KV budget 2048 plus generation reserve 1024 per lane,
  64 MiB CPU arena per host store, no NVMe, user query policy, 32 HTTP threads.
- CUDA graphs and llama graph reuse enabled with their normal defaults.
  No launch-blocking or diagnostic graph-disable environment variables.
- Approximately 1900-token repeated prompts with an ID-specific secret word.
  Short replies request the secret; long replies request it followed by counting.
  Temperature is zero, and 48 serial requests establish short/long output hashes
  for all 24 IDs before load begins.
- A quarter of requests target four hot IDs to exercise same-ID contention.
  The ramps use eight IDs; churn uses 24, forcing reclamation with an eight-store
  limit. Requests mix short replies, 192-token replies, and longer streams
  deliberately disconnected after 24 tokens. Invalid-type and oversized requests
  are injected every 15 seconds and must return HTTP 400.

The script selects `CUDA_VISIBLE_DEVICES=0` with `CUDA_DEVICE_ORDER=PCI_BUS_ID`,
so its CUDA0 is the RTX 5050 reported as index 0 by `nvidia-smi`. The other card,
an RTX 5060 Ti, stayed at its initial 12 MiB usage throughout the measured run.

## Results

Phase durations include draining in-flight requests. Throughput counts tokens
from completed requests only and excludes work from deliberately canceled
streams. First-token and completion latency include queue and prompt processing.

| Phase | Clients | Duration (s) | Completed | Canceled | Expected HTTP 400 | Output tokens/s | First-token P95 (s) | Completion P95 (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Ramp | 2 | 62.917 | 46 | 2 | 3 | 122.21 | 1.539 | 3.473 |
| Ramp | 4 | 62.996 | 54 | 1 | 3 | 125.36 | 5.579 | 8.325 |
| Ramp | 8 | 66.626 | 51 | 3 | 3 | 124.17 | 9.543 | 12.376 |
| Ramp | 16 | 77.764 | 65 | 3 | 3 | 123.84 | 18.808 | 21.248 |
| Ten-minute churn | 16 | 617.056 | 469 | 22 | 39 | 115.73 | 22.245 | 24.621 |

Including calibration and recovery, 757 requests completed, 31 were deliberately
canceled, and 53 were rejected as expected. There were no request/monitor errors,
secret-isolation failures, or output-hash mismatches. All 685 completed load
requests and 24 recovery requests matched their serial output hashes. All 24
IDs answered correctly after load, both lanes became idle, and subsequent invalid
requests changed neither the switch nor eviction counters.

The host-store count never exceeded eight. Final counters reported 723 switches,
236 evictions, 544 extends and 244 forks. During churn, both lanes were active in
593 of 594 monitor samples; they completed 229 and 240 requests respectively.

Device-wide sampled GPU usage peaked at 2134 MiB (2.08 GiB), temperature at
72 degrees C, and churn utilization averaged 98.3%. Usage returned to zero when
the script stopped its server. Whole-run sampled working-set/private-byte peaks
were 2831.35/5142.87 MiB. Churn's first-minute medians were 2677.93/4995.68 MiB,
and its last-minute medians were 2642.27/4959.77 MiB. No sustained memory growth
was observed in this run; these samples do not establish absence of leaks.

Additional clients mainly increased queue latency once the two lanes were
saturated. The churn phase also spends more work rebuilding evicted prompts.
This is a stability/correctness workload, not a comparison against single-lane
throughput or a recommendation for a production client limit.

## Bug found and correction

The initial shorter test reproduced a CUDA illegal-memory-access crash. Disabling
CUDA graph capture alone did not prevent it; disabling llama's decode-graph reuse
did. Compute Sanitizer on the failing build identified an invalid write in
`k_set_rows_quant<long long, block_q8_0>` with corrupted row indices.

Cached decode widths share a backend scheduler arena. Reactivating a dormant
graph after another graph has run can leave its tensor addresses pointing into
the other graph's temporary or split-input storage. The existing reactivation
branch treated these stale addresses as reusable allocations.

`patches/cuda-graph-reactivation.patch` removes that unsafe branch. A graph is
reused only when it is still the active graph; dormant graphs go through the
existing synchronized reset, rebuild, KVMem capture registration and allocation
path. Consecutive uses of the active decode graph continue to reuse it. This
does not disable CUDA graphs or serialize the two lanes. Alternating graph
widths, including MTP widths, may incur additional rebuilding.

The measured build includes fix commit
`6f1bf82a048daf7355b036bd38dc3fb7d42b60a1`. After that fix:

| Regression | Result |
|---|---|
| Windows model-free CTest | 16/16 passed |
| Default-graph short load smoke, 4/8/16 clients | 103 completed, zero errors |
| Existing single-lane conversations, 0.8B on RTX 5050 | 137/137 passed |
| Same-GPU projector, two lanes and MTP, 27B on RTX 5060 Ti | 38/38 passed |
| PR #94 two-lane 512-token decode/disconnect regression, 27B + MTP | 14/14 passed |
| Maintained patch series on a clean pinned llama.cpp source, then reapplied | Passed |
| GitHub CI for the fix commit, Linux/Windows host, UI and Linux CUDA build | 7/7 jobs passed |

The 27B regression also checked encoding a cold image while both lanes decoded,
cross-lane host-KV restoration, same-ID queue recovery and image/text isolation.
The sustained RTX 5050 run used text without MTP or a projector. Multi-hour runs,
growing chat histories, and sustained multimedia/MTP pressure remain unmeasured.
Compute Sanitizer diagnosed the failing build; a full post-fix sanitizer run was
not performed.

## Reproduce and inspect

From the repository root, using a supplied model and a CUDA server build:

```powershell
python scripts/stress_server_lane_conversations.py `
  --server build-win/bin/llama-kvmem-server.exe `
  --model ../models/Qwen3.5-0.8B-Q8_0.gguf `
  --output artifacts/stress-5050-new-run `
  --clients 2,4,8,16 --ramp-seconds 60 --soak-seconds 600 `
  --conversations 8 --logical-conversations 24
```

Use a fresh output directory. Change `--cuda-visible` and `--gpu-index` together
for another card. The script creates an ephemeral loopback server, downloads
nothing, and terminates only its own server. This run's local evidence is in
`artifacts/stress-5050-full-2/`: `results.json`, per-request `requests.jsonl`,
one-second `samples.jsonl`, and `server.log`. Regression evidence is in
`artifacts/single-lane-conversations-stress-fix/` and
`artifacts/lane-conversations-vision-gpu-stress-fix/`. Artifacts are ignored by Git.
