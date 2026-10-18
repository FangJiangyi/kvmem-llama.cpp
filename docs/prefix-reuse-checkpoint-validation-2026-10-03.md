# LCP-boundary checkpoint contribution and validation provenance

[Iscream132](https://github.com/Iscream132) diagnosed the repeated full-prefill
failure in [#101](https://github.com/kvmem/kvmem/issues/101), proposed keeping a
recurrent checkpoint at the longest common prefix (LCP), and contributed the
original three-turn regression in [#102](https://github.com/kvmem/kvmem/pull/102).
The original commit is `5d1ac0abfb3d902fbd2eeca3c3bb20ce2a1a799e`; its authorship
and history are preserved by the integration of #102.

When an agent replaces its trailing user reminder with an assistant/tool turn,
the new common prefix can end just before every retained checkpoint. The original
fix splits prefill at that boundary and saves a checkpoint, while leaving the
query checkpoint and query replay origin separate. This problem analysis, fix
and reproducible request pattern are the contribution carried into the current
implementation.

## Original evidence and corrections

The original three-turn reproduction checks native `no_recurrent_checkpoint`
and `prefix_hit_rows` traces. The author withdrew an earlier end-to-end
performance comparison after finding mixed-run figures and incomplete failure
reporting. The [October 3 correction](https://github.com/kvmem/kvmem/pull/102#issuecomment-5970491551)
and [October 4 same-revision A/B follow-up](https://github.com/kvmem/kvmem/issues/101#issuecomment-5978213775)
describe the evidence and its limits.

Those measurements describe the author's historical builds, not current master.
This integration makes no new performance claim and does not carry forward the
withdrawn figures as a current benchmark.

## Current integration and regression

[#137](https://github.com/kvmem/kvmem/pull/137) introduced shared checkpoint
planning and retention for llama.cpp and NInfer. It carries observed LCP through
cold admission, gives reuse anchors an explicit retention purpose, and leaves
native token/media/KV/state checks with each backend. The active contract is in
[memory-backend-contract.md](memory-backend-contract.md#lcp-与前缀-checkpoint).
The old `tools/kvmem-multimodal-server.h` is superseded by the current driver and
is not restored by this integration.

`tests/prefix-reuse-regression.py` retains the author's trailing-message rewrite
scenario, uses actual server replies to build the next request, and requires
nonzero native prefix reuse from the third turn onward. There are no timing or
exact generated-text assertions; valid EOS completion is accepted.

- `kvmem-prefix-reuse-regression`: six turns, four retained sessions, ordinary
  decoding. Set `KVMEM_TEST_MODEL` and `KVMEM_TEST_GPU` (GPU UUID).
- `kvmem-prefix-reuse-mtp-regression`: twelve turns, one retained session, MTP3;
  also requires draft verification to occur. Set `KVMEM_TEST_MTP_MODEL` to an
  MTP-capable GGUF and set `KVMEM_TEST_GPU`.

Both CTest entries skip with code 77 when their model or GPU is not configured
and run serially to avoid loading both models on the same GPU concurrently.
These regressions establish prefix recovery, not comparative throughput or a
guarantee that arbitrary history edits reuse state on their first occurrence.
