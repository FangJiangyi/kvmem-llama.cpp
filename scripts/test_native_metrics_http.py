"""Native contract smoke test; owns one loopback worker and an explicit GPU."""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

def request_json(url, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + path, timeout=30) as response:
        return json.load(response)



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["llamacpp", "ninfer"], required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mtp", action="store_true")
    parser.add_argument("--canary", action="store_true", help="also exercise speed_canary HTTP reporting")
    parser.add_argument("--mtp-state", choices=["replay", "snapshots"], default="replay")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    if args.backend == "llamacpp":
        command = [str(args.server), "-m", str(args.model), "--host", "127.0.0.1",
            "--port", str(port), "-c", "2048", "-b", "128", "-ub", "128", "-ngl", "99",
            "--metrics", "--no-ui", "--no-think", "--kv-dtype", "q8_0"]
        if args.mtp:
            command += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3",
                        "--spec-kv-dtype", "f16", "--kvmem-mtp-state", args.mtp_state]
    else:
        command = [str(args.server), str(args.model), "--host", "127.0.0.1",
            "--port", str(port), "--max-context", "2048", "--prefill-chunk", "128",
            "--device", "0", "--device-profile", "off", "--kv-dtype", "int8", "--no-thinking"]
    if args.backend == "ninfer" and args.mtp:
        command += ["--spec", "mtp", "--draft-tokens", "3"]
    command += ["--kvmem", "--kvmem-budget", "1024", "--kvmem-gen-reserve", "256",
                "--kvmem-host-mib", "256", "--kvmem-disk-mib", "0"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KVMEM_", "NINFER_", "GGML_", "LLAMA_ARG_"))}
    env.update(CUDA_VISIBLE_DEVICES=args.gpu, CUDA_DEVICE_ORDER="PCI_BUS_ID", GGML_DISABLE_VULKAN="1")
    env["PATH"] = str(args.server.parent) + os.pathsep + env["PATH"]
    evidence = {"command": command, "gpu_uuid": args.gpu, "checks": [], "responses": []}
    def check(label, condition):
        evidence["checks"].append({"name": label, "pass": bool(condition)})
        print(("PASS " if condition else "FAIL ") + label, flush=True)
        if not condition:
            raise AssertionError(label)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(body, path="/v1/chat/completions"):
        return opener.open(urllib.request.Request(url + path,
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=120)
    def settled():
        for _ in range(60):
            rows = request_json(url, "/api/monitor/snapshot")["active"]
            if not rows:
                return
            time.sleep(.05)
        raise AssertionError("worker did not retire request")
    def validate(body):
        t, u = body["timings"], body["usage"]
        n = u["completion_tokens"]
        check("native token accounting", n == t["predicted_n"] and
              u["prompt_tokens"] == t["prompt_n"] + t["cache_n"])
        expected = 1000 * max(n - 1, 0) / t["predicted_ms"] if t["predicted_ms"] else 0
        check("native first-token timing convention", math.isclose(t["predicted_per_second"], expected, rel_tol=1e-9))
        if args.mtp:
            s = body["kvmem"]["speculative"]
            check("MTP native draft fields", t.get("draft_n", 0) == s["drafted_tokens"] and
                  t.get("draft_n_accepted", 0) == s["accepted_tokens"] <= s["drafted_tokens"])
            check("MTP accepted positions reconcile", sum(s["accepted_per_position"]) == s["accepted_tokens"])
            if args.backend == "llamacpp":
                check("MTP rounds reconcile", s["generation_rounds"] == s["verification_steps"] +
                      s["restore_rounds"] + s["fallback_rounds"] + s["uncommitted_rounds"])
            else:
                check("MTP native verification rounds", s["generation_rounds"] >= s["verification_steps"])
        evidence["responses"].append(body)

    with (args.output / "server.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 180
            while True:
                if process.poll() is not None:
                    raise RuntimeError("worker exited; inspect server.log")
                try:
                    if request_json(url, "/health")["status"] == "ok":
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("worker startup")
                time.sleep(.1)
            check("metrics capability advertised", request_json(url, "/props")["endpoint_metrics"])
            payload = {"messages": [{"role": "user", "content": "Count integers from 1 to 20."}],
                       "temperature": 0, "seed": 42, "max_tokens": 16}
            with request(payload) as response:
                validate(json.load(response))
            with request(dict(payload, max_tokens=1)) as response:
                one = json.load(response)
                validate(one)
                check("one token does not inflate throughput", one["timings"]["predicted_per_second"] == 0)
            settled()
            check("monitor uses native one-token rate",
                  request_json(url, "/api/monitor/snapshot")["recent"][0]["decode_tps"] == 0)
            for include_usage in [False, True]:
                events = []
                with request(dict(payload, stream=True, stream_options={"include_usage": include_usage})) as response:
                    for line in response:
                        if not line.startswith(b"data: "):
                            continue
                        item = line[6:].strip()
                        if item == b"[DONE]":
                            break
                        events.append(json.loads(item))
                final = events[-1]
                check("stream ends with native timing", "timings" in final)
                if args.mtp:
                    check("stream terminal MTP diagnostics", "speculative" in final.get("kvmem", {}))
                check("stream usage opt-in", bool(final.get("usage")) == include_usage)
                check("per-token timings opt-in", all("timings" not in e for e in events[:-1]))
                if include_usage:
                    validate(final)
            try:
                with request({"messages": "invalid"}):
                    pass
                check("invalid request rejected", False)
            except urllib.error.HTTPError as error:
                check("invalid request rejected", error.code == 400)
            # Disconnect during generation, then check native gauges and reuse the worker.
            with request(dict(payload, stream=True, max_tokens=256)) as response:
                for line in response:
                    if line.startswith(b"data: {"):
                        event = json.loads(line[6:])
                        if any(c.get("delta", {}).get("content") for c in event.get("choices", [])):
                            break
            settled()
            check("cancelled stream retires", not request_json(url, "/api/monitor/snapshot")["active"])
            terminal = None
            with request({"model": request_json(url, "/props")["model_alias"],
                          "input": "Reply OK.", "max_output_tokens": 8, "stream": True}, "/v1/responses") as response:
                for line in response:
                    if line.startswith(b"data: {"):
                        event = json.loads(line[6:])
                        if event.get("type") in ("response.completed", "response.incomplete"):
                            terminal = event
            check("Responses stream completes after cancellation", terminal is not None)
            if args.backend == "llamacpp":
                check("Responses terminal native timings", "timings" in terminal)
            else:
                check("Responses retains namespaced diagnostics", "kvmem" in terminal["response"])
            with opener.open(url + "/metrics", timeout=30) as response:
                text = response.read().decode()
            (args.output / "metrics.prom").write_text(text, encoding="utf-8")
            check("native metrics exported", "llamacpp:tokens_predicted_total " in text)
            if args.mtp:
                if args.backend == "llamacpp":
                    check("speculative metrics exported", "llamacpp:spec_decode_num_drafts_total " in text and
                          "llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position=" in text)
                else:
                    check("speculative metrics exported", "ninfer:draft_tokens_total " in text and
                          "ninfer:draft_accepted_tokens_total " in text)
            if args.canary:
                # Exercise the actual report client against this worker without a speed gate.
                report = args.output / "canary.json"
                subprocess.run([sys.executable, str(Path(__file__).with_name("speed_canary.py")),
                    "--url", url, "--backend", args.backend, "--runs", "1", "--warmup", "0",
                    "--prompt-repeats", "2", "-n", "16", "--output", str(report)], check=True)
                check("report retains native fields", "native" in json.loads(report.read_text())["runs"][0])
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=30)
            (args.output / "results.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
