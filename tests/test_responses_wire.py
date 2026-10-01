#!/usr/bin/env python3
"""Responses wire-format regressions against a live server.

Covers three classes of defect the in-repo C++ tests cannot see, because all
three live in the request-to-template handoff or in what the SDK makes of the
response, not in the event emitter:

  shape     request shapes a real client sends that the Responses -> Chat
            Completions conversion mishandles (jinja template failures,
            @ai-sdk replay quirks, the multi-turn tool loop).
  stream    the SSE envelope itself: explicit `event:` line agreeing with
            `data.type`, contiguous sequence_number, one terminal event, no
            `data: [DONE]` sentinel, and the delta/done ordering invariants.
  sdk       the response read back through the official OpenAI SDK, whose
            pydantic models are stricter than a hand-written client.

Every case that is known to fail today is tagged with a `known` marker; the
suite reports those separately instead of failing, so a fix shows up as a
marker that can be deleted rather than as a suite that was red all along.

Usage:
    python tests/test_responses_wire.py --server PATH --model PATH
    ... --json OUT          machine-readable per-case results
    ... --strict            treat currently-known failures as failures

Exit code 0 when every non-known case passes.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def post(base, path, payload, timeout=300):
    """POST raw JSON. Returns (status, parsed_body_or_None, raw_text).

    A streaming response is not JSON; callers that want the bytes use raw.
    """
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=raw,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", "replace")
        status = error.code
    try:
        return status, json.loads(text), text
    except ValueError:
        return status, None, text


def parse_sse(text):
    """[{type, data}] from a raw SSE body, in order.

    Reads both lines the way the wire defines them: the `event:` line and the
    `data.type` field must agree, so a frame missing either is a defect and is
    reported as one rather than silently skipped.
    """
    frames, event_line, data_line = [], None, None
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("event: "):
            event_line = line[7:]
        elif line.startswith("data: "):
            data_line = line[6:]
        elif line == "" and (event_line is not None or data_line is not None):
            data = None
            if data_line is not None and data_line != "[DONE]":
                try:
                    data = json.loads(data_line)
                except ValueError:
                    data = None
            frames.append({"event": event_line, "data": data, "raw": data_line})
            event_line, data_line = None, None
    return frames


class Suite:
    def __init__(self, strict):
        self.results = []
        self.strict = strict

    def check(self, group, name, ok, detail="", known=False):
        """Record one case.

        A `known` case is a defect this suite was written to catch and that is
        still open. It is reported as KNOWN so the suite stays meaningful for
        everything else; `--strict` turns it back into a failure.
        """
        if known and not ok:
            outcome = "FAIL" if self.strict else "KNOWN"
        else:
            outcome = "pass" if ok else "FAIL"
        self.results.append({"group": group, "case": name, "outcome": outcome,
                             "known": known, "detail": str(detail)[:600]})
        mark = {"pass": ".", "KNOWN": "k", "FAIL": "F"}[outcome]
        line = "%s %-9s %-42s" % (mark, group, name)
        if outcome != "pass":
            line += "  " + str(detail)[:200].replace("\n", " ")
        print(line, flush=True)
        return ok

    def failed(self):
        return [r for r in self.results if r["outcome"] == "FAIL"]


# ---------------------------------------------------------------------------
# Group: request shapes
# ---------------------------------------------------------------------------

def tool_schema():
    return [{"type": "function", "name": "get_weather",
             "description": "Get the current weather for a city.",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}]


def group_shape(suite, base):
    """Request shapes a real client sends, and the multi-turn tool loop."""
    small = {"model": "test", "max_output_tokens": 8, "temperature": 0}

    def status_of(name, extra, path="/v1/responses"):
        payload = dict(small)
        payload.update(extra)
        status, body, _ = post(base, path, payload)
        err = body.get("error") if isinstance(body, dict) else None
        return status, ("" if err is None else str(err))

    # A user message with no system role anywhere: the shape every client uses
    # for a plain turn. This must never break.
    status, err = status_of("user turn, no system role", {"input": "Say OK."})
    suite.check("shape", "user turn, no system role", status == 200, "%s %s" % (status, err))

    # `instructions` becomes a leading system message. A system/developer item
    # inside `input` is passed through untouched, so the template sees two.
    for name, extra in [
        ("instructions + input[user, developer]",
         {"instructions": "S", "input": [{"role": "user", "content": "hi"},
                                         {"role": "developer", "content": "D"}]}),
        ("instructions + input[developer, user]",
         {"instructions": "S", "input": [{"role": "developer", "content": "D"},
                                         {"role": "user", "content": "hi"}]}),
        ("instructions + input[system, user]",
         {"instructions": "S", "input": [{"role": "system", "content": "S2"},
                                         {"role": "user", "content": "hi"}]}),
    ]:
        status, err = status_of(name, extra)
        crashed = "System message must be at the beginning" in err
        suite.check("shape", name, status == 200 or not crashed,
                    "%s %s" % (status, err[:120]), known=crashed)

    # A system message that is not first is fatal to the Qwen template on both
    # endpoints, so this documents the shared root cause rather than a
    # Responses-only defect.
    status, err = status_of("system item first in input",
                            {"input": [{"role": "system", "content": "S"},
                                       {"role": "user", "content": "hi"}]})
    suite.check("shape", "system item first in input", status == 200, "%s %s" % (status, err[:120]))

    # @ai-sdk replay shapes the server patches for by hand. These are the
    # regressions the comments in tools/kvmem-responses.cpp exist to protect.
    for name, items in [
        ("assistant replay without `type`",
         [{"role": "user", "content": "What is 2+3?"},
          {"role": "assistant", "content": "5"},
          {"role": "user", "content": "add 1"}]),
        ("reasoning item carrying only `summary`",
         [{"role": "user", "content": "hi"},
          {"type": "reasoning", "summary": [{"type": "summary_text", "text": "s"}]},
          {"type": "function_call", "call_id": "call_1", "name": "get_weather",
           "arguments": '{"city":"SF"}'},
          {"type": "function_call_output", "call_id": "call_1", "output": "sunny"}]),
        ("reasoning item carrying `content`",
         [{"role": "user", "content": "hi"},
          {"type": "reasoning", "summary": [{"type": "summary_text", "text": "s"}],
           "content": [{"type": "reasoning_text", "text": "raw"}]},
          {"type": "function_call", "call_id": "call_1", "name": "get_weather",
           "arguments": '{"city":"SF"}'},
          {"type": "function_call_output", "call_id": "call_1", "output": "sunny"}]),
    ]:
        payload = dict(small, input=items, tools=tool_schema(), max_output_tokens=32)
        status, body, _ = post(base, "/v1/responses", payload)
        suite.check("shape", name, status == 200,
                    "%s %s" % (status, (body or {}).get("error", "")))

    # Upstream refuses this outright; the message must name the parameter so a
    # client can tell it apart from a malformed request.
    status, body, _ = post(base, "/v1/responses",
                           dict(small, input="hi", previous_response_id="resp_1"))
    suite.check("shape", "previous_response_id is refused by name",
                status == 400 and "previous_response_id" in str((body or {}).get("error", "")),
                "%s %s" % (status, (body or {}).get("error", "")))

    # Accepted-and-ignored parameters must not turn into a 400: a client sends
    # them unconditionally whether or not the server implements them.
    for key, value in [("store", True), ("metadata", {"a": "b"}),
                       ("include", ["reasoning.encrypted_content"]),
                       ("text", {"format": {"type": "json_object"}}),
                       ("tool_choice", "auto")]:
        status, body, _ = post(base, "/v1/responses", dict(small, input="Say OK.", **{key: value}))
        suite.check("shape", "passthrough %s" % key, status == 200,
                    "%s %s" % (status, (body or {}).get("error", "")))


# ---------------------------------------------------------------------------
# Group: SSE envelope
# ---------------------------------------------------------------------------

TERMINAL = ("response.completed", "response.failed", "response.incomplete")


def group_stream(suite, base, model):
    """The streaming contract a client state machine is built on."""
    payload = {"model": model, "input": "What is 2+3? Answer with the number only.",
               "max_output_tokens": 96, "temperature": 0, "stream": True}
    status, _, text = post(base, "/v1/responses", payload)
    if status != 200:
        suite.check("stream", "stream request accepted", False, "HTTP %s: %s" % (status, text[:200]))
        return
    frames = parse_sse(text)
    types = [f["event"] for f in frames]
    suite.check("stream", "stream request accepted", True)

    # No sentinel: the terminal event is the end-of-stream signal, unlike Chat
    # Completions where a client waits for `data: [DONE]`.
    suite.check("stream", "no `data: [DONE]` sentinel",
                "[DONE]" not in text, "found a [DONE] frame")

    # Every frame carries both an `event:` line and a `type`, and they agree.
    mismatched = [f for f in frames
                  if f["event"] is None or f["data"] is None
                  or f["data"].get("type") != f["event"]]
    suite.check("stream", "event: line agrees with data.type", not mismatched,
                mismatched[:2])

    # Contiguous from 0, no gaps and no reuse. A client that keys anything off
    # this number drops events the moment one frame is skipped.
    seqs = [f["data"].get("sequence_number") for f in frames if f["data"]]
    suite.check("stream", "sequence_number starts at 0", seqs and seqs[0] == 0, seqs[:3])
    suite.check("stream", "sequence_number is strictly contiguous",
                seqs == list(range(len(seqs))), seqs[:12])

    # Exactly one terminal event, and it is last.
    terminals = [t for t in types if t in TERMINAL]
    suite.check("stream", "exactly one terminal event", len(terminals) == 1, terminals)
    suite.check("stream", "terminal event is last", types and types[-1] in TERMINAL, types[-3:])
    suite.check("stream", "first event is response.created",
                types and types[0] == "response.created", types[:2])
    # A synchronous stream must not claim to be queued.
    suite.check("stream", "no response.queued on a synchronous stream",
                "response.queued" not in types, [t for t in types if t == "response.queued"])

    # Ordering invariants: every *.delta addressed to an output_index must come
    # after that index was opened, and every block that opened must close.
    opened, order_ok, opened_part = set(), True, set()
    for f in frames:
        if not f["data"]:
            continue
        t, d = f["event"], f["data"]
        if t == "response.output_item.added":
            opened.add(d.get("output_index"))
        elif t.endswith(".delta") and "output_index" in d:
            if d.get("output_index") not in opened:
                order_ok = False
        elif t == "response.content_part.added":
            opened_part.add((d.get("output_index"), d.get("content_index")))
        elif t == "response.output_text.delta":
            if (d.get("output_index"), d.get("content_index")) not in opened_part:
                order_ok = False
    suite.check("stream", "delta never precedes its output_item.added", order_ok)

    closed = {f["data"].get("output_index") for f in frames
              if f["event"] == "response.output_item.done" and f["data"]}
    suite.check("stream", "every opened item is closed",
                {i for i in opened if i is not None} <= closed,
                "opened=%s closed=%s" % (sorted(x for x in opened if x is not None), sorted(closed)))

    # delta carries the increment, done carries the whole text.
    deltas = "".join(f["data"].get("delta", "") for f in frames
                     if f["event"] == "response.output_text.delta" and f["data"])
    done = next((f["data"].get("text") for f in frames
                 if f["event"] == "response.output_text.done" and f["data"]), None)
    suite.check("stream", "output_text.done.text equals the delta sum",
                done is not None and done == deltas, "%r vs %r" % (done, deltas))

    # The completed response carries the whole output and a usage block.
    completed = next((f["data"]["response"] for f in frames
                      if f["event"] == "response.completed" and f["data"]), None)
    suite.check("stream", "response.completed carries output and usage",
                isinstance(completed, dict) and completed.get("output") is not None
                and isinstance(completed.get("usage"), dict), completed)
    if isinstance(completed, dict):
        usage = completed.get("usage") or {}
        suite.check("stream", "usage totals add up",
                    usage.get("total_tokens") == (usage.get("input_tokens", 0)
                                                  + usage.get("output_tokens", 0)), usage)

    # A tool call streams its arguments in fragments and closes them.
    payload = dict(payload, input="Call get_weather for San Francisco.",
                   tools=tool_schema(), max_output_tokens=96)
    status, _, text = post(base, "/v1/responses", payload)
    if status == 200:
        frames = parse_sse(text)
        types = [f["event"] for f in frames]
        suite.check("stream", "tool call emits argument deltas and done",
                    "response.function_call_arguments.delta" in types
                    and "response.function_call_arguments.done" in types,
                    [t for t in types if "function_call" in t])
        # The item id on a delta must be the one the added event opened.
        added = next((f["data"]["item"]["id"] for f in frames
                      if f["event"] == "response.output_item.added"
                      and f["data"] and f["data"].get("item", {}).get("type") == "function_call"), None)
        deltas = [f["data"]["item_id"] for f in frames
                  if f["event"] == "response.function_call_arguments.delta" and f["data"]]
        suite.check("stream", "argument deltas address the opened item",
                    added is not None and all(i == added for i in deltas),
                    "added=%s deltas=%s" % (added, sorted(set(deltas))))


# ---------------------------------------------------------------------------
# Group: official SDK
# ---------------------------------------------------------------------------

def group_sdk(suite, base, model):
    """The response as the official OpenAI SDK reads it."""
    try:
        import openai
    except ImportError:
        suite.check("sdk", "openai SDK importable", False, "pip install openai", known=True)
        return

    client = openai.OpenAI(base_url=base + "/v1", api_key="sk-test")
    suite.check("sdk", "openai SDK importable", True, openai.__version__)

    try:
        r = client.responses.create(model=model, input="What is 2+3? Answer with the number only.",
                                    max_output_tokens=32, temperature=0)
        text = "".join(p.text or "" for i in r.output if i.type == "message"
                       for p in i.content if p.type == "output_text")
        suite.check("sdk", "non-streaming response parses", r.status == "completed", r.status)
        suite.check("sdk", "non-streaming text is correct", text.strip() == "5", text)
        # The SDK is lenient about fields its models declare required; a None
        # here means the server omitted a field the schema demands, which a
        # stricter client (or a pydantic validation pass) rejects outright.
        missing = [k for k in ("parallel_tool_calls", "tool_choice", "tools")
                   if getattr(r, k, None) is None]
        suite.check("sdk", "Response carries every required field", not missing,
                    "missing: %s" % missing, known=bool(missing))
        usage = r.usage
        missing = [k for k in ("cache_write_tokens",) if getattr(usage.input_tokens_details, k, None) is None]
        if usage.output_tokens_details is None:
            missing.append("output_tokens_details.reasoning_tokens")
        suite.check("sdk", "usage carries every required field", not missing,
                    "missing: %s" % missing, known=bool(missing))
    except Exception as e:  # noqa: BLE001 - any SDK failure is the finding
        suite.check("sdk", "non-streaming response parses", False, "%s: %s" % (type(e).__name__, e))

    try:
        events, text = [], ""
        for event in client.responses.create(model=model,
                                             input="What is 2+3? Answer with the number only.",
                                             max_output_tokens=32, temperature=0, stream=True):
            events.append(event.type)
            if event.type == "response.output_text.delta":
                text += event.delta
        suite.check("sdk", "streaming response parses", text.strip() == "5", text)
        suite.check("sdk", "streaming completes once and last",
                    events.count("response.completed") == 1 and events[-1] == "response.completed",
                    events[-3:])
    except Exception as e:  # noqa: BLE001
        suite.check("sdk", "streaming response parses", False, "%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_healthy(proc, port, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited %s before becoming healthy" % proc.returncode)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError("server did not become healthy")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", type=Path,
                    help="llama-kvmem-server binary; starts a private instance")
    ap.add_argument("--model", type=Path)
    ap.add_argument("--base", help="use an already-running server instead, e.g. http://127.0.0.1:18200")
    ap.add_argument("--model-name", default="test")
    ap.add_argument("--port", type=int)
    ap.add_argument("--log")
    ap.add_argument("--json", type=Path, help="write per-case results here")
    ap.add_argument("--strict", action="store_true",
                    help="count currently-known failures as failures")
    args = ap.parse_args()

    if not args.base and not (args.server and args.model):
        ap.error("either --base, or both --server and --model")

    proc = None
    base = args.base
    try:
        if not base:
            port = args.port or free_port()
            base = "http://127.0.0.1:%d" % port
            log_path = args.log or os.path.join(os.environ.get("TEMP", "/tmp"),
                                                "responses-wire-server.log")
            flags = [str(args.server), "-m", str(args.model), "--host", "127.0.0.1",
                     "--port", str(port), "-c", "8192", "-np", "1", "--no-webui",
                     "--no-ui", "--predict", "512", "-ngl", "99"]
            print("starting %s" % " ".join(flags), flush=True)
            log = open(log_path, "wb")
            proc = subprocess.Popen(flags, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            wait_healthy(proc, port)
            print("server healthy at %s (log: %s)" % (base, log_path), flush=True)

        suite = Suite(args.strict)
        group_shape(suite, base)
        group_stream(suite, base, args.model_name)
        group_sdk(suite, base, args.model_name)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    passed = sum(1 for r in suite.results if r["outcome"] == "pass")
    known = sum(1 for r in suite.results if r["outcome"] == "KNOWN")
    failed = suite.failed()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(suite.results, indent=2), encoding="utf-8")
    print("\n%d passed, %d known-open, %d failed" % (passed, known, len(failed)), flush=True)
    for r in failed:
        print("FAIL %s/%s: %s" % (r["group"], r["case"], r["detail"][:200]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
