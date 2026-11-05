"""A scripted OpenAI-compatible endpoint so the container acceptance test needs no real model.

The last user message selects the behavior: ``run: <cmd>`` calls the exec tool, ``tools`` lists the tool names the
request offered, ``probe <text>`` reports whether ``<text>`` appears anywhere earlier in the prompt, and anything else
answers ``done``. A tool result is echoed back as ``tool said: <output>``.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _answer(body: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    messages = body.get("messages") or []
    last = messages[-1]
    text = _text(last.get("content")).split("\n\n[Runtime Context", 1)[0].strip()
    if last.get("role") == "tool":
        return f"tool said: {text[:4000]}", None
    if text.startswith("run: "):
        return "", {"command": text[5:]}
    if text == "tools":
        names = [t["function"]["name"] for t in body.get("tools") or []]
        return "tools=" + ",".join(names), None
    if text.startswith("probe "):
        needle = text[6:]
        earlier = " ".join(_text(m.get("content")) for m in messages[:-1])
        return f"probe={needle in earlier}", None
    return "done", None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # model listing probes
        self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        content, call = _answer(body)
        usage = {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128}
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if call is not None:
            message["tool_calls"] = [{
                "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                "function": {"name": "exec", "arguments": json.dumps(call)},
            }]
        finish = "tool_calls" if call is not None else "stop"
        if not body.get("stream"):
            self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "model": "fake-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage,
            })
            return
        delta = dict(message)
        if call is not None:
            delta["tool_calls"] = [{**message["tool_calls"][0], "index": 0}]
        chunks = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": usage},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            payload = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": "fake-model", **chunk}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_llm() -> tuple[ThreadingHTTPServer, int]:
    """Listen on all interfaces (the gateway container reaches it through the host) and return the port."""
    server = ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])
