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

Local validation uses the RTX 5050 Laptop (`GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`), CUDA
13.2 and MSVC 19.44. No performance comparison is part of this integration. Full-model acceptance
and the contributor's 256K recall/speed results require DFlash2 weights and are not established by
the kernel and state tests.
