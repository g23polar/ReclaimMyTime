"""Agent configuration, built via `AgentSettings.from_env()` (nothing reads env at import).

The API key is not here: the SDK client resolves ANTHROPIC_API_KEY itself (Fly secret).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSettings:
    # ARCH §8: an upgrade is a config change (prompt caches are per-model).
    model: str = "claude-haiku-5-5"
    max_tokens: int = 16000  # room for adaptive thinking plus a reply; non-streaming-safe
    max_model_calls: int = 8  # per user turn (decision 2026-11-15); hitting it ends the turn
    heartbeat_seconds: float = 15.0  # contract: `: ping` at least every 15 s
    open_timeout_seconds: float = 30.0  # first model call, retries included → else 503
    turn_timeout_seconds: float = 120.0  # whole turn after the 200 → else `error`

    @classmethod
    def from_env(cls) -> AgentSettings:
        return cls(model=os.environ.get("AGENT_MODEL", cls.model))
