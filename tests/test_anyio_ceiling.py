"""requirements.txt caps anyio below 4.16 for the mcp 1.x SDK.

The SDK's standalone SSE writer (the long GET /mcp stream) reads a memory
stream. When Cloud Run cuts that request at 120 s, the stream is closed
while the writer waits on it. Under anyio 4.15 the waiting reader stays put
until its task is cancelled, which is quiet. Under 4.16 it is woken with
ClosedResourceError, which mcp 1.x doesn't catch, and each cut logged an
ERROR traceback on vibetrader-mcp-00069-rs8 (2026-11-07 → 11-09).

This pins the behaviour the SDK relies on, so lifting the ceiling without
moving to mcp 2 fails here rather than in the logs.
"""

import anyio


def test_closing_a_stream_under_a_waiting_reader_raises_nothing():
    raised = []

    async def main():
        send, receive = anyio.create_memory_object_stream(0)

        async def reader():
            try:
                async for _ in receive:
                    pass
            except anyio.ClosedResourceError as exc:
                raised.append(exc)

        with anyio.move_on_after(0.5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                await anyio.sleep(0.05)   # the reader is now waiting
                receive.close()           # the request was cut under it
                await anyio.sleep(0.2)
        send.close()

    anyio.run(main)
    assert not raised, (
        "this anyio wakes a closed stream's reader with ClosedResourceError; "
        "mcp 1.x's standalone_sse_writer would log it as an ERROR on every cut stream"
    )
