"""Hand-written Anthropic Messages streaming responses behind an httpx2 MockTransport
(decision 2026-11-15: no recorded or live fixtures). Each scripted response is one model
call; the transport records every request body so tests can assert on what was sent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2


@dataclass
class Text:
    text: str


@dataclass
class Tool:
    name: str
    input: dict[str, Any]
    id: str = ""


@dataclass
class Call:
    """One model response: content blocks plus a stop reason."""

    blocks: list[Text | Tool]
    stop_reason: str = "end_turn"
    thinking: bool = True  # Haiku 5.5 thinks by default; blocks must be echoed back


@dataclass
class Failure:
    status: int = 529
    body: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "x"},
        }
    )


def _sse(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def stream_body(call: Call, n: int) -> str:
    out = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            },
        )
    ]
    index = 0
    if call.thinking:
        out.append(
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
        )
        out.append(
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": f"sig{n}"},
                },
            )
        )
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    for k, block in enumerate(call.blocks):
        if isinstance(block, Text):
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block.text}]
        else:
            tid = block.id or f"toolu_{n}_{k}"
            start = {"type": "tool_use", "id": tid, "name": block.name, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block.input)}]
        out.append(
            _sse(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            )
        )
        for d in deltas:
            out.append(
                _sse(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": d},
                )
            )
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    out.append(
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": call.stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 20},
            },
        )
    )
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(out)


@dataclass
class Hang:
    """An upstream that never answers (until `seconds`)."""

    seconds: float = 30.0


class FakeAnthropic:
    def __init__(self, *script: Call | Failure | Hang) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def handler(self) -> Callable[[httpx2.Request], Awaitable[httpx2.Response]]:
        async def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(json.loads(request.content))
            n = len(self.requests)
            if n > len(self.script):
                raise AssertionError(f"unexpected model call #{n}")
            step = self.script[n - 1]
            if isinstance(step, Hang):
                await asyncio.sleep(step.seconds)
            if isinstance(step, Hang):
                return httpx2.Response(529, json=Failure().body)
            if isinstance(step, Failure):
                return httpx2.Response(step.status, json=step.body)
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=stream_body(step, n).encode(),
            )

        return handle

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler())),
        )
