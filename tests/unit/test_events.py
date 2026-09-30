from __future__ import annotations

import asyncio
import json

from epitaph.events import EventBus, control, make_event, subscribe


async def test_subscribe_gets_snapshot_then_events() -> None:
    bus = EventBus(port=0, snapshot=lambda: make_event("snapshot", 3, words=[]))
    await bus.start()
    got: list[dict] = []

    async def reader() -> None:
        async for e in subscribe(port=bus.port):
            got.append(e)
            if len(got) == 3:
                return

    task = asyncio.create_task(reader())
    for _ in range(50):
        if bus.subscriber_count:
            break
        await asyncio.sleep(0.01)
    bus.publish(make_event("birth", 3))
    bus.publish(make_event("word", 3, text="hello"))
    await asyncio.wait_for(task, 2)
    assert [e["type"] for e in got] == ["snapshot", "birth", "word"]
    assert got[0]["v"] == 1
    await bus.stop()


async def test_control_commands() -> None:
    bus = EventBus(port=0)

    async def status(args: dict) -> dict:
        return {"life": 7, "echo": args}

    bus.on("status", status)
    await bus.start()
    reply = await control("status", {"x": 1}, port=bus.port)
    assert reply == {"reply": "status", "life": 7, "echo": {"x": 1}}
    unknown = await control("nope", port=bus.port)
    assert "error" in unknown
    await bus.stop()


async def test_slow_subscriber_never_blocks_and_gets_snapshot() -> None:
    bus = EventBus(port=0, queue_size=5, snapshot=lambda: make_event("snapshot", 1))
    await bus.start()
    _reader, writer = await asyncio.open_connection("127.0.0.1", bus.port)
    writer.write(b'{"subscribe": true}\n')
    await writer.drain()
    for _ in range(50):
        if bus.subscriber_count:
            break
        await asyncio.sleep(0.01)
    sub = next(iter(bus._subs))
    sub.task.cancel()  # simulate a subscriber that never reads
    for i in range(100):
        bus.publish(make_event("word", 1, i=i))  # must return immediately
    assert sub.overflows > 0
    assert sub.queue.qsize() <= 5
    items = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert any(e["type"] == "snapshot" for e in items)
    writer.close()
    await bus.stop()


async def test_bad_json_is_answered_not_fatal() -> None:
    bus = EventBus(port=0)
    await bus.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", bus.port)
    writer.write(b"not json\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 2)
    assert json.loads(line)["error"] == "bad json"
    writer.close()
    await bus.stop()
