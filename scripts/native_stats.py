"""Read native backend responses; normalize only the benchmark report."""
from __future__ import annotations

import json
import math
import urllib.request


def speculative_report(raw: dict | None) -> dict:
    if raw is None:
        return {"available": False, "unavailable_reason": "backend_did_not_report_rounds"}
    out = dict(raw)
    for key in ("drafted_tokens", "accepted_tokens", "generation_rounds", "output_tokens"):
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"invalid speculative counter: {key}")
    drafted, accepted = raw["drafted_tokens"], raw["accepted_tokens"]
    if accepted > drafted:
        raise ValueError("accepted draft tokens exceed proposed draft tokens")
    rounds, tokens = raw["generation_rounds"], raw["output_tokens"]
    wall_ms = raw.get("wall_ms")
    if not isinstance(wall_ms, (int, float)) or not math.isfinite(wall_ms) or wall_ms < 0:
        raise ValueError("invalid speculative wall time")
    out.update(available=rounds > 0,
               acceptance_rate=accepted / drafted if drafted else None,
               tokens_per_round=tokens / rounds if rounds else None,
               ms_per_round=wall_ms / rounds if rounds else None)
    if not rounds:
        out.setdefault("unavailable_reason", "no_decode_rounds_observed")
    return out


def response_report(body: dict, backend: str) -> dict:
    timings = body.get("timings", {})
    usage = body.get("usage", {})
    raw_spec = body.get("kvmem", {}).get("speculative")
    spec = speculative_report(raw_spec)
    # Native upstreams may expose acceptance without exposing round counts.
    # Never infer rounds from output tokens minus accepted drafts (EOS/replay/fallback).
    if raw_spec is None and "draft_n" in timings:
        drafted, accepted = timings["draft_n"], timings.get("draft_n_accepted", 0)
        spec.update(drafted_tokens=drafted, accepted_tokens=accepted,
                    acceptance_rate=accepted / drafted if drafted else None)
    return {"backend": backend, "prompt_n": timings.get("prompt_n"),
            "gen_n": usage.get("completion_tokens", timings.get("predicted_n")),
            "prompt_ms": timings.get("prompt_ms"), "gen_ms": timings.get("predicted_ms"),
            "prompt_toks": timings.get("prompt_per_second"),
            "gen_toks": timings.get("predicted_per_second"),
            "speculative": spec, "native": {"usage": usage, "timings": timings}}


def request_json(base_url: str, path: str, body: dict | None = None,
                 timeout: float = 180, api_key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers,
        data=None if body is None else json.dumps(body).encode())
    # Local workers must not inherit a machine-wide HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)


def http_series(args, prompt: str) -> tuple[list[dict], dict]:
    """Compare explicitly configured servers without changing their native settings."""
    import os
    key = os.environ.get("KVMEM_API_KEY")
    records = []
    metadata = {"backend": args.backend, "seed": args.seed, "max_tokens": args.n_predict,
                "temperature": 0, "warmup": args.warmup, "runs": args.runs}
    for label, url in (("baseline", args.baseline_url), ("candidate", args.url)):
        if not url:
            continue
        props = request_json(url, "/props", timeout=args.timeout, api_key=key)
        metadata[label] = {"url": url, "props": props}
        model = props.get("model_alias")
        payload = {"messages": [{"role": "user", "content": prompt}],
                   "max_tokens": args.n_predict, "seed": args.seed, "temperature": 0}
        if model:
            payload["model"] = model
        for index in range(args.warmup + args.runs):
            body = request_json(url, "/v1/chat/completions", payload, args.timeout, key)
            record = response_report(body, args.backend)
            if index < args.warmup:
                continue
            record.update(label=label, run=index - args.warmup)
            records.append(record)
            spec = record["speculative"]
            def number(value, digits=2):
                return "n/a" if value is None else f"{value:.{digits}f}"
            print(f"{label:10s} run={record['run']} tok/s={number(record['gen_toks'])} "
                  f"ms/round={number(spec.get('ms_per_round'))} "
                  f"tokens/round={number(spec.get('tokens_per_round'))} "
                  f"accept={number(None if spec.get('acceptance_rate') is None else 100 * spec['acceptance_rate'])}%")
    return records, metadata
