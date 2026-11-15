"""SSE framing + heartbeat for POST /agent/message."""

import asyncio

from app.agent.router import frame, sse


def test_frame_is_single_line_json():
    assert frame(("delta", {"text": "Thu 6–8 PM\nOK?"})) == (
        'event: delta\ndata: {"text":"Thu 6–8 PM\\nOK?"}\n\n'
    )


async def test_heartbeat_while_waiting_then_events():
    async def slow():
        await asyncio.sleep(0.25)
        yield "done", {"outcome": "completed"}

    out = [chunk async for chunk in sse(slow(), heartbeat=0.1)]
    assert out[:2] == [": ping\n\n", ": ping\n\n"]
    assert out[-1].startswith("event: done\n")


async def test_client_disconnect_cancels_the_turn():
    closed = asyncio.Event()

    async def turn():
        try:
            yield "delta", {"text": "a"}
            await asyncio.sleep(10)
            yield "done", {}
        finally:
            closed.set()

    stream = sse(turn(), heartbeat=5)
    assert (await anext(stream)).startswith("event: delta")
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    pending.cancel()  # Starlette cancels the body iterator when the client goes away
    await asyncio.gather(pending, return_exceptions=True)
    await stream.aclose()
    assert closed.is_set()
