"""
Drop-in HTTP shim. Point any OpenAI-compatible client's base_url at it.

    POST /v1/chat/completions   -> governed, then forwarded upstream
    POST /signal                -> push grid stress or a curtailment order
    GET  /state                 -> effort, elimination, live proof
    GET  /proof                 -> the datasheet claim
    GET  /metrics               -> Prometheus text format
    GET  /healthz

Streaming is first-class, because real traffic is SSE and a driver that only
works on buffered responses only works in demos. Three things follow:

  * TTFT is measured from the FIRST BYTE of the first chunk, which is the
    number a user actually feels. Measuring total latency would let a driver
    hide a slow start behind a fast finish.
  * A streamed response is reassembled into canonical form as it passes, so it
    can be cached without storing provider-specific chunk framing.
  * A cache hit on a streaming request is re-emitted AS a stream. Handing a
    client a buffered body when it asked for SSE breaks it, and "the cache
    only works if you don't stream" is not a product.

Stdlib only: http.server and urllib. No framework, no wheels to audit.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .sidechain import Sidechain
from .signal import CurtailmentOrder, GridSignal, WebhookSource

CHAT_PATHS = ("/chat/completions", "/messages", "/completions")


def canonical_from_body(body: dict) -> dict:
    """Provider-shaped response -> the minimum we need to replay it."""
    try:
        ch = (body.get("choices") or [{}])[0]
        content = (ch.get("message") or {}).get("content")
        if content is None:
            blocks = body.get("content") or []
            content = "".join(b.get("text", "") for b in blocks
                              if isinstance(b, dict))
        return {"content": content or "", "model": body.get("model", ""),
                "usage": body.get("usage") or {}}
    except Exception:
        return {"content": "", "model": body.get("model", ""), "usage": {}}


def render_buffered(canon: dict) -> dict:
    return {"id": "loadslack-cache", "object": "chat.completion",
            "created": int(time.time()), "model": canon.get("model", ""),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": canon.get("content", "")}}],
            "usage": canon.get("usage") or {}, "loadslack_cached": True}


def render_sse(canon: dict) -> bytes:
    """Re-emit a cached answer as a well-formed SSE stream."""
    model, now = canon.get("model", ""), int(time.time())

    def chunk(delta, finish=None):
        return "data: " + json.dumps({
            "id": "loadslack-cache", "object": "chat.completion.chunk",
            "created": now, "model": model,
            "choices": [{"index": 0, "delta": delta,
                         "finish_reason": finish}]}) + "\n\n"

    return ("".join([chunk({"role": "assistant"}),
                     chunk({"content": canon.get("content", "")}),
                     chunk({}, "stop"), "data: [DONE]\n\n"])).encode()


def assemble_sse(chunks: list) -> dict:
    """Reassemble streamed deltas into canonical form."""
    parts, model = [], ""
    for raw in chunks:
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            model = d.get("model") or model
            for c in d.get("choices") or []:
                t = (c.get("delta") or {}).get("content")
                if t:
                    parts.append(t)
            delta = d.get("delta") or {}
            if isinstance(delta, dict) and delta.get("text"):
                parts.append(delta["text"])
    return {"content": "".join(parts), "model": model, "usage": {}}


def extract_prompt(body: dict) -> Tuple[str, list]:
    msgs = body.get("messages") or []
    if msgs:
        bits = []
        sysp = body.get("system")
        if isinstance(sysp, str):
            bits.append(f"system: {sysp}")
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                bits.append(f"{m.get('role', '')}: {c}")
            elif isinstance(c, list):
                bits.append(f"{m.get('role', '')}: " + " ".join(
                    b.get("text", "") for b in c if isinstance(b, dict)))
        return "\n".join(bits), msgs
    return str(body.get("prompt") or ""), []


class _Handler(BaseHTTPRequestHandler):
    server_version = "loadslack/0.3"
    sc: Sidechain = None
    upstream: str = ""
    webhook: Optional[WebhookSource] = None
    dry_run: bool = False

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _fwd_headers(self) -> dict:
        h = {k: v for k, v in self.headers.items()
             if k.lower() in ("authorization", "x-api-key",
                              "anthropic-version", "openai-organization")}
        h["Content-Type"] = "application/json"
        return h

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if u.path == "/state":
            return self._json(200, self.sc.state())
        if u.path == "/proof":
            return self._json(200, {"datasheet": self.sc.datasheet(),
                                    **self.sc.verifier.proof()})
        if u.path == "/report":
            q = parse_qs(u.query)
            since = float(q.get("since", [time.time() - 86400])[0])
            return self._json(200, self.sc.ledger.event_report(since))
        if u.path == "/metrics":
            return self._metrics()
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path == "/signal":
            return self._signal()
        if any(u.path.endswith(p) for p in CHAT_PATHS):
            return self._completions(u.path)
        self._json(404, {"error": "not found"})

    def _signal(self) -> None:
        if self.webhook is None:
            return self._json(400, {"error": "no webhook source configured"})
        raw = self._read_body()
        order = None
        if isinstance(raw.get("order"), dict):
            o, now = raw["order"], time.time()
            order = CurtailmentOrder(
                source=o.get("source", "webhook"),
                issued_at=float(o.get("issued_at", now)),
                expires_at=float(o.get("expires_at",
                                       now + float(o.get("duration_s", 3600)))),
                reduction_fraction=_f(o.get("reduction_fraction")),
                target_mw=_f(o.get("target_mw")), shed_mw=_f(o.get("shed_mw")),
                event_id=str(o.get("event_id", "")))
        self.webhook.push(GridSignal(
            stress=float(raw.get("stress", 0.0)),
            region=str(raw.get("region", "webhook")),
            carbon_g_per_kwh=_f(raw.get("carbon_g_per_kwh")), order=order))
        self._json(202, {"accepted": True, "grid": self.sc.state()["grid"]})

    def _completions(self, path: str) -> None:
        body = self._read_body()
        site = (self.headers.get("X-LoadSlack-Site")
                or body.get("loadslack_site") or body.get("model") or "default")
        prompt, msgs = extract_prompt(body)
        want_stream = bool(body.get("stream"))
        max_tok = int(body.get("max_tokens")
                      or body.get("max_output_tokens") or 0)

        t0 = time.perf_counter()
        turn = self.sc.before(site, prompt, messages=msgs, max_tokens=max_tok,
                              request=body,
                              key=self.headers.get("X-LoadSlack-Key")
                              or f"{site}:{prompt[:256]}")

        if turn.served:
            canon = turn.response if isinstance(turn.response, dict) else {}
            ttft = (time.perf_counter() - t0) * 1000.0
            self.sc.after(turn, ttft_ms=ttft, total_ms=ttft)
            if want_stream:
                payload = render_sse(canon)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-LoadSlack-Served", turn.rot.served_from)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            return self._json(200, {**render_buffered(canon),
                                    "loadslack": {
                                        "served_from": turn.rot.served_from}})

        if turn.coalesce_ms:
            time.sleep(turn.coalesce_ms / 1000.0)

        fwd = dict(turn.request)
        if self.dry_run or not self.upstream:
            ttft = (time.perf_counter() - t0) * 1000.0
            self.sc.after(turn, None, ttft_ms=ttft, total_ms=ttft)
            return self._json(200, {"dry_run": True, "forwarded": fwd,
                                    "loadslack": {"effort": turn.effort,
                                                 "stress": turn.stress}})

        req = urllib.request.Request(
            self.upstream.rstrip("/") + path, data=json.dumps(fwd).encode(),
            headers=self._fwd_headers(), method="POST")
        try:
            if want_stream:
                self._relay_stream(req, turn, t0)
            else:
                self._relay_buffered(req, turn, t0)
        except urllib.error.HTTPError as e:
            self.sc.after(turn, None)
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            self.sc.after(turn, None)
            self._json(502, {"error": {"type": "upstream_error",
                                       "message": str(e)}})

    def _relay_buffered(self, req, turn, t0) -> None:
        with urllib.request.urlopen(req, timeout=600) as r:
            raw = r.read()
            ttft = (time.perf_counter() - t0) * 1000.0
            self.send_response(r.status)
            self.send_header("Content-Type",
                             r.headers.get("Content-Type", "application/json"))
            self.send_header("X-LoadSlack-Effort", f"{turn.effort:.3f}")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return self.sc.after(turn, None, ttft_ms=ttft, total_ms=ttft)
        canon = canonical_from_body(parsed)
        self.sc.after(turn, canon,
                      output_tokens=int((canon.get("usage") or {}).get(
                          "completion_tokens", 0))
                      or max(1, len(canon["content"]) // 4),
                      ttft_ms=ttft,
                      total_ms=(time.perf_counter() - t0) * 1000.0)

    def _relay_stream(self, req, turn, t0) -> None:
        """
        Relay chunks as they arrive. TTFT is the first byte out of upstream,
        not the last -- that is the number the user feels, and measuring
        anything else would let the driver hide a slow start.
        """
        chunks, ttft = [], None
        with urllib.request.urlopen(req, timeout=600) as r:
            self.send_response(r.status)
            self.send_header("Content-Type",
                             r.headers.get("Content-Type",
                                           "text/event-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-LoadSlack-Effort", f"{turn.effort:.3f}")
            self.end_headers()
            while True:
                buf = r.read1(8192) if hasattr(r, "read1") else r.read(8192)
                if not buf:
                    break
                if ttft is None:
                    ttft = (time.perf_counter() - t0) * 1000.0
                chunks.append(buf)
                try:
                    self.wfile.write(buf)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return self.sc.after(turn, None)
        canon = assemble_sse(chunks)
        total = (time.perf_counter() - t0) * 1000.0
        if not canon["content"]:
            return self.sc.after(turn, None, ttft_ms=ttft or total,
                                 total_ms=total)
        self.sc.after(turn, canon,
                      output_tokens=max(1, len(canon["content"]) // 4),
                      ttft_ms=ttft or total, total_ms=total)

    def _metrics(self) -> None:
        s = self.sc.state()
        rot, proof = s["rot"], s["proof"]
        led, d = proof["latency_ledger"], proof.get("ttft_delta_ms") or {}
        lines = [
            "# TYPE loadslack_grid_stress gauge",
            f"loadslack_grid_stress {s['grid']['stress']}",
            "# TYPE loadslack_effort gauge",
            f"loadslack_effort {s['effort']}",
            "# TYPE loadslack_grid_coupled gauge",
            f"loadslack_grid_coupled {1 if s['grid']['coupled'] else 0}",
            "# TYPE loadslack_eliminations_total counter",
            f"loadslack_eliminations_total {rot['eliminations']}",
            "# TYPE loadslack_refusals_total counter",
            f"loadslack_refusals_total {rot['refusals']}",
            "# TYPE loadslack_cache_entries gauge",
            f"loadslack_cache_entries {rot['cache_entries']}",
            "# TYPE loadslack_inflight_collapsed_total counter",
            f"loadslack_inflight_collapsed_total {rot['inflight']['collapsed']}",
            "# TYPE loadslack_inflight_timeouts_total counter",
            f"loadslack_inflight_timeouts_total {rot['inflight']['timeouts']}",
            "# TYPE loadslack_latency_net_ms gauge",
            f"loadslack_latency_net_ms {led['net_ms']}",
            "# TYPE loadslack_latency_invariant_holds gauge",
            f"loadslack_latency_invariant_holds {1 if led['invariant_holds'] else 0}",
            "# TYPE loadslack_ttft_delta_ms gauge",
            f"loadslack_ttft_delta_ms {d.get('point', 0)}",
            "# TYPE loadslack_verdict gauge",
        ]
        for v in ("PASS_FASTER", "PASS_INDISTINGUISHABLE", "INSUFFICIENT_DATA",
                  "FAIL_SLOWER", "FAIL_IDENTITY"):
            lines.append(f'loadslack_verdict{{verdict="{v}"}} '
                         f'{1 if proof["verdict"] == v else 0}')
        payload = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(sc: Sidechain, host: str = "127.0.0.1", port: int = 8787,
          upstream: str = "", webhook: Optional[WebhookSource] = None,
          dry_run: bool = False) -> ThreadingHTTPServer:
    handler = type("Bound", (_Handler,), {
        "sc": sc, "upstream": upstream, "webhook": webhook,
        "dry_run": dry_run})
    return ThreadingHTTPServer((host, port), handler)


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
