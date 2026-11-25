"""AgentSettings.from_env (issue #3: the daily token budget)."""

from __future__ import annotations

import pytest

from app.agent.settings import AgentSettings


def test_daily_token_budget_defaults_to_one_million(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_DAILY_TOKEN_BUDGET", raising=False)
    assert AgentSettings().daily_token_budget == 1_000_000
    assert AgentSettings.from_env().daily_token_budget == 1_000_000


def test_daily_token_budget_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DAILY_TOKEN_BUDGET", "5000")
    assert AgentSettings.from_env().daily_token_budget == 5000


def test_daily_token_budget_must_be_an_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_DAILY_TOKEN_BUDGET", "lots")
    with pytest.raises(ValueError):
        AgentSettings.from_env()
