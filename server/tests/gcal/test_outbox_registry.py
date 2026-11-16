"""OutboxRegistry (T4.3a) in isolation: one task per user, kicks during a drain re-run it (no
pass cap), `wait` is bounded and never cancels, and a crashing drain never escapes. No DB, no
Google: the drain is a scripted coroutine. Every await is timeout-guarded."""

from __future__ import annotations

import asyncio
import uuid

from app.gcal.writeback import DrainResult, DrainStatus, OutboxRegistry

A = uuid.UUID(int=1)
B = uuid.UUID(int=2)


class Script:
    """A drain that records each pass and parks on a per-pass gate until released."""

    def __init__(self) -> None:
        self.passes: list[uuid.UUID] = []
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.raise_on: set[int] = set()

    async def __call__(self, user_id: uuid.UUID) -> DrainResult:
        self.passes.append(user_id)
        n = len(self.passes)
        self.started.set()
        await self.gate.wait()
        if n in self.raise_on:
            raise RuntimeError("boom")
        return DrainResult(DrainStatus.DONE, written=n)


async def test_one_task_per_user_and_users_are_independent() -> None:
    script = Script()
    reg = OutboxRegistry(script)
    a1 = reg.kick(A)
    assert reg.kick(A) is a1  # before the pass starts: folded into it, no extra pass
    await asyncio.wait_for(script.started.wait(), 5)  # A's first pass is running
    assert reg.kick(A) is a1  # mid-pass: A runs once more
    b1 = reg.kick(B)
    assert a1 is not b1
    script.gate.set()
    await asyncio.wait_for(reg.idle(), 5)
    assert not reg.running(A) and not reg.running(B)
    assert sorted(script.passes) == [A, A, B]


async def test_kicks_during_every_pass_are_never_dropped() -> None:
    """Five passes in a row each receive a kick: the task keeps going (no cap), and one
    final pass runs after the last kick."""
    calls = 0
    gates: asyncio.Queue[asyncio.Event] = asyncio.Queue()

    async def drain(user_id: uuid.UUID) -> DrainResult:
        nonlocal calls
        calls += 1
        gate = asyncio.Event()
        gates.put_nowait(gate)
        await gate.wait()
        return DrainResult(DrainStatus.DONE, written=calls)

    reg = OutboxRegistry(drain)
    task = reg.kick(A)
    for _ in range(5):
        gate = await asyncio.wait_for(gates.get(), 5)  # a pass is running
        assert reg.kick(A) is task
        gate.set()
    (await asyncio.wait_for(gates.get(), 5)).set()
    result = await asyncio.wait_for(task, 5)
    assert calls == 6 and result.written == 6
    assert not reg.running(A)


async def test_wait_is_bounded_and_does_not_cancel_the_drain() -> None:
    script = Script()
    reg = OutboxRegistry(script)
    assert await asyncio.wait_for(reg.wait(A, 0.05), 5) is None
    assert reg.running(A)
    script.gate.set()
    await asyncio.wait_for(reg.idle(), 5)
    assert script.passes  # finished in the background


async def test_wait_returns_the_result_when_done_in_time() -> None:
    script = Script()
    script.gate.set()
    reg = OutboxRegistry(script)
    result = await asyncio.wait_for(reg.wait(A, 5), 5)
    assert result == DrainResult(DrainStatus.DONE, written=1)


async def test_a_crashing_drain_is_contained_and_the_next_kick_starts_fresh() -> None:
    script = Script()
    script.raise_on = {1}
    script.gate.set()
    reg = OutboxRegistry(script)
    first = await asyncio.wait_for(reg.kick(A), 5)
    assert first.status is DrainStatus.UNAVAILABLE
    second = await asyncio.wait_for(reg.kick(A), 5)
    assert second == DrainResult(DrainStatus.DONE, written=2)
