#!/usr/bin/env python3
"""P1 speed canary: KVMem-on vs KVMem-off on RTX 5050.

Regression report, not a race against kvmem-qw3. A large tok/s drop is
reported for a human go/no-go; it does not fail the script unless
--fail-on-drop is set. Crashes / wrong GPU still abort.

CLI mode selects the RTX 5050 by UUID via gpu_env. HTTP mode reads native
responses from explicitly configured workers; it does not launch or reconfigure them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_env  # noqa: E402
from native_stats import http_series, speculative_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / "models/unsloth/Qwen3-0.6B-GGUF/Qwen3-0.6B-Q8_0.gguf"
PERF_RE = re.compile(
    r"KVMEM_PERF load_ms=(?P<load_ms>[\d.]+) prompt_n=(?P<prompt_n>\d+) "
    r"prompt_ms=(?P<prompt_ms>[\d.]+) prompt_toks=(?P<prompt_toks>[\d.]+) "
    r"gen_n=(?P<gen_n>\d+) gen_ms=(?P<gen_ms>[\d.]+) gen_toks=(?P<gen_toks>[\d.]+)"
)


def find_cli() -> Path:
    for p in (
        ROOT / "build/bin/llama-kvmem-cli.exe",
        ROOT / "build/bin/llama-kvmem-cli",
        ROOT / "build/llama-kvmem-cli",
    ):
        if p.is_file():
            return p
    raise SystemExit("llama-kvmem-cli not found; run scripts/build-cuda.sh")


@dataclass
class Perf:
    load_ms: float
    prompt_n: int
    prompt_ms: float
    prompt_toks: float
    gen_n: int
    gen_ms: float
    gen_toks: float
    speculative: dict = field(default_factory=lambda: speculative_report(None))


def parse_perf(stderr: str) -> Perf:
    m = PERF_RE.search(stderr)
    if not m:
        raise SystemExit("missing KVMEM_PERF line in llama-kvmem-cli stderr")
    result = Perf(
        load_ms=float(m.group("load_ms")),
        prompt_n=int(m.group("prompt_n")),
        prompt_ms=float(m.group("prompt_ms")),
        prompt_toks=float(m.group("prompt_toks")),
        gen_n=int(m.group("gen_n")),
        gen_ms=float(m.group("gen_ms")),
        gen_toks=float(m.group("gen_toks")),
    )
    summaries = re.findall(r"^KVMEM_SPEC_METRICS (.+)$", stderr, re.MULTILINE)
    if summaries:
        if len(summaries) != 1:
            raise ValueError("expected one speculative summary per CLI request")
        result.speculative = speculative_report(json.loads(summaries[0]))
        # libllama n_eval counts verification calls, not committed MTP output.
        # Use one matching output-count / generation-wall-time interval instead.
        result.gen_n = result.speculative["output_tokens"]
        result.gen_ms = result.speculative["wall_ms"]
        result.gen_toks = 1000 * result.gen_n / result.gen_ms if result.gen_ms > 0 else 0
    return result


def run_once(cli: Path, model: Path, extra: list[str], prompt: str, n_predict: int, n_ctx: int) -> Perf:
    env = gpu_env.apply_gpu(os.environ.copy(), "small")
    cmd = [
        str(cli),
        "-m", str(model),
        "-n", str(n_predict),
        "-c", str(n_ctx),
        "-b", "256",
        "-ngl", "99",
        "--no-prompt",
        *extra,
        prompt,
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"command failed rc={proc.returncode}")
    gpu_env.require_device(proc.stderr, "RTX 5050")
    return parse_perf(proc.stderr)


def mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def summarize(label: str, runs: list[Perf]) -> dict[str, float]:
    out = {
        "prompt_toks": mean([r.prompt_toks for r in runs]),
        "gen_toks": mean([r.gen_toks for r in runs]),
        "load_ms": mean([r.load_ms for r in runs]),
        "prompt_n": mean([float(r.prompt_n) for r in runs]),
        "gen_n": mean([float(r.gen_n) for r in runs]),
    }
    print(
        f"{label:12s}  prompt {out['prompt_toks']:8.1f} tok/s "
        f"({out['prompt_n']:.0f} tok)   decode {out['gen_toks']:8.1f} tok/s "
        f"({out['gen_n']:.0f} tok)   load {out['load_ms']:.0f} ms"
    )
    specs = [r.speculative for r in runs if r.speculative.get("available")]
    if specs:
        rounds = sum(s["generation_rounds"] for s in specs)
        drafted = sum(s["drafted_tokens"] for s in specs)
        accepted = sum(s["accepted_tokens"] for s in specs)
        accept = f"{100 * accepted / drafted:.1f}%" if drafted else "n/a"
        print(f"              {sum(s['wall_ms'] for s in specs) / rounds:.2f} ms/round  "
              f"{sum(s['output_tokens'] for s in specs) / rounds:.3f} tokens/round  "
              f"draft acceptance={accept}")
    return out


def ratio(on: float, off: float) -> float:
    return on / off if off > 0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("-n", "--n-predict", type=int, default=128)
    ap.add_argument("--prompt-repeats", type=int, default=48,
                    help="repeat a sentence this many times to size the prompt")
    ap.add_argument("--ctx", type=int, default=1024)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-drop", type=float, default=0.15,
                    help="warn (or fail with --fail-on-drop) if identity tok/s drops more than this")
    ap.add_argument("--fail-on-drop", action="store_true",
                    help="exit 1 when identity tok/s drop exceeds --max-drop")
    ap.add_argument("--backend", choices=("llamacpp", "ninfer"), default="llamacpp")
    ap.add_argument("--url", help="HTTP worker base URL; read its native statistics")
    ap.add_argument("--baseline-url", help="optional separately configured baseline worker")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--output", type=Path, help="save metadata and individual runs as JSON")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cli", type=Path)
    ap.add_argument("--spec-type", choices=("none", "draft-mtp"), default="none",
                    help="CLI mode only; HTTP mode uses the worker's native configuration")
    ap.add_argument("--spec-draft-n-max", type=int, default=2)
    ap.add_argument("--spec-kv-dtype", help="CLI mode: native draft KV type")
    ap.add_argument("--mtp-state", choices=("replay", "snapshots"), help="CLI mode: KVMem MTP state strategy")
    args = ap.parse_args()
    if args.runs < 1 or args.warmup < 0 or not 0 <= args.seed < 4294967295:
        ap.error("runs must be positive, warmup nonnegative, seed in 0..4294967294")
    prompt = ("The quick brown fox jumps over the lazy dog. ") * args.prompt_repeats
    prompt += "Write a short continuation:"
    if args.url:
        if args.fail_on_drop:
            ap.error("HTTP reports do not apply a regression gate across independently configured workers")
        records, metadata = http_series(args, prompt)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"metadata": metadata, "runs": records}, indent=2), encoding="utf-8")
        return 0
    if args.backend != "llamacpp" or args.baseline_url:
        ap.error("ninfer and baseline-url require --url; ninfer's native statistics are read over HTTP")
    if not args.model.is_file():
        raise SystemExit(f"missing model {args.model}")

    cli = args.cli or find_cli()
    records = {}
    common = ["--seed", str(args.seed)]
    if args.spec_type != "none":
        common += ["--spec-type", args.spec_type, "--spec-draft-n-max", str(args.spec_draft_n_max)]
    if args.spec_kv_dtype:
        common += ["--spec-kv-dtype", args.spec_kv_dtype]
    if args.mtp_state:
        common += ["--kvmem-mtp-state", args.mtp_state]

    def series(extra: list[str]) -> list[Perf]:
        for _ in range(args.warmup):
            run_once(cli, args.model, common + extra, prompt, args.n_predict, args.ctx)
        return [
            run_once(cli, args.model, common + extra, prompt, args.n_predict, args.ctx)
            for _ in range(args.runs)
        ]

    print(f"model={args.model.name} ctx={args.ctx} n_predict={args.n_predict} "
          f"warmup={args.warmup} runs={args.runs} device=RTX 5050")
    off = series([])
    on = series(["--kvmem", "--kvmem-method", "recency"])  # budget 0 → n_ctx, no capture
    tight = series(["--kvmem", "--kvmem-method", "recency",
                    "--kvmem-budget", "256", "--kvmem-gen-reserve", "128"])
    # Product default is retrieval (capture + query replay).
    retr_ident = series(["--kvmem"])
    retr_tight = series(["--kvmem", "--kvmem-budget", "256", "--kvmem-gen-reserve", "128"])
    if args.output:
        records = {label: [asdict(r) for r in runs] for label, runs in
                   (("off", off), ("recency_identity", on), ("recency_tight", tight),
                    ("retrieval_identity", retr_ident), ("retrieval_tight", retr_tight))}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"metadata": {"backend": "llamacpp", "cli": str(cli),
            "model": str(args.model.resolve()), "seed": args.seed, "ctx": args.ctx,
            "n_predict": args.n_predict, "spec_type": args.spec_type,
            "gpu_uuid": gpu_env.UUID_5050,
            "source_backend_versions": json.loads((ROOT / "backends/versions.json").read_text(encoding="utf-8")),
            "spec_kv_dtype": args.spec_kv_dtype, "mtp_state": args.mtp_state,
            "spec_draft_n_max": args.spec_draft_n_max, "runs": args.runs, "warmup": args.warmup},
            "runs": records}, indent=2), encoding="utf-8")

    print()
    s_off = summarize("off", off)
    s_on = summarize("rec ident", on)
    s_tight = summarize("rec tight", tight)
    s_ri = summarize("retr ident", retr_ident)
    s_rt = summarize("retr tight", retr_tight)

    drop_gen = 1.0 - ratio(s_on["gen_toks"], s_off["gen_toks"])
    drop_pre = 1.0 - ratio(s_on["prompt_toks"], s_off["prompt_toks"])
    print()
    print(f"recency  identity decode  on/off = {ratio(s_on['gen_toks'], s_off['gen_toks']):.3f}  drop={drop_gen:.1%}")
    print(f"recency  identity prefill on/off = {ratio(s_on['prompt_toks'], s_off['prompt_toks']):.3f}  drop={drop_pre:.1%}")
    print(f"recency  tight    decode  on/off = {ratio(s_tight['gen_toks'], s_off['gen_toks']):.3f}  (informational)")
    print(f"retrieve identity decode  on/off = {ratio(s_ri['gen_toks'], s_off['gen_toks']):.3f}  drop={1.0 - ratio(s_ri['gen_toks'], s_off['gen_toks']):.1%}")
    print(f"retrieve identity prefill on/off = {ratio(s_ri['prompt_toks'], s_off['prompt_toks']):.3f}  drop={1.0 - ratio(s_ri['prompt_toks'], s_off['prompt_toks']):.1%}")
    print(f"retrieve tight    decode  on/off = {ratio(s_rt['gen_toks'], s_off['gen_toks']):.3f}  (informational)")
    print(f"retrieve tight    prefill on/off = {ratio(s_rt['prompt_toks'], s_off['prompt_toks']):.3f}  (informational)")

    if s_on["gen_n"] < args.n_predict * 0.5 or s_off["gen_n"] < args.n_predict * 0.5:
        print("FAIL: too few generated tokens (early EOS?)", file=sys.stderr)
        return 1

    big_drop = drop_gen > args.max_drop or drop_pre > args.max_drop
    if not big_drop:
        print(f"OK: identity-budget KVMem within {args.max_drop:.0%} of stock llama.cpp")
        return 0

    print()
    print("GO/NO-GO: identity-budget KVMem is slower than stock llama.cpp.")
    print(f"  decode  drop {drop_gen:.1%}  (threshold {args.max_drop:.0%})")
    print(f"  prefill drop {drop_pre:.1%}  (threshold {args.max_drop:.0%})")
    print("  This is a report, not an automatic stop. Decide whether to continue.")
    if args.fail_on_drop:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
