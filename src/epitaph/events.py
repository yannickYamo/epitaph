"""Event bus and control channel: JSON lines over TCP on 127.0.0.1.

Every connection may subscribe to events, send control commands, or both:
  -> {"subscribe": true}              the server sends a snapshot, then every event
  -> {"cmd": "status", "args": {...}} the server replies {"reply": "status", ...}

Each subscriber has its own bounded queue drained by its own task, so publishing never
waits on a subscriber. On overflow the queue is cleared and a fresh snapshot is queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from epitaph.types import PROTOCOL_VERSION

Event = dict[str, Any]
SnapshotFn = Callable[[], Event]
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def make_event(etype: str, life: int, **fields: Any) -> Event:
    """Build an event stamped with the protocol version and the wall-clock time in seconds."""
    return {
        "v": PROTOCOL_VERSION,
        "ts": round(time.time(), 3),
        "life": life,
        "type": etype,
        **fields,
    }


class _Subscriber:
    """One subscribed connection: a bounded queue and the task that writes it out."""

    def __init__(self, writer: asyncio.StreamWriter, maxsize: int, snapshot: SnapshotFn) -> None:
        self.writer = writer
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.snapshot = snapshot
        self.overflows = 0
        self.task: asyncio.Task[None] | None = None

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflows += 1
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(self.snapshot())

    async def pump(self) -> None:
        try:
            while True:
                event = await self.queue.get()
                self.writer.write((json.dumps(event) + "\n").encode())
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass


class EventBus:
    """Publishes events to local subscribers and answers control commands."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7707,
        queue_size: int = 2000,
        snapshot: SnapshotFn | None = None,
    ) -> None:
        """Listen on host:port (port 0 picks a free one at `start`).

        queue_size bounds each subscriber's queue in events; snapshot builds the state sent to
        new and overflowed subscribers.
        """
        self.host = host
        self.port = port
        self.queue_size = queue_size
        self.snapshot: SnapshotFn = snapshot or (lambda: make_event("snapshot", 0))
        self.handlers: dict[str, Handler] = {}
        self._subs: set[_Subscriber] = set()
        self._server: asyncio.Server | None = None
        self._local: list[Callable[[Event], None]] = []

    # -- publishing -----------------------------------------------------------------------

    def publish(self, event: Event) -> None:
        """Queue an event for every subscriber. Never blocks."""
        for fn in self._local:
            fn(event)
        for sub in list(self._subs):
            sub.offer(event)

    def add_local(self, fn: Callable[[Event], None]) -> None:
        """In-process listener (transcripts, tests). Called synchronously; keep it cheap."""
        self._local.append(fn)

    def on(self, cmd: str, handler: Handler) -> None:
        """Register the handler for a control command, replacing any earlier one.

        The handler gets the command's args and returns the reply fields; an exception it
        raises becomes an error reply.
        """
        self.handlers[cmd] = handler

    @property
    def subscriber_count(self) -> int:
        """Number of connected TCP subscribers (in-process listeners are not counted)."""
        return len(self._subs)

    # -- server ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Start listening; afterwards `port` holds the real port even if 0 was asked for."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        if self.port == 0:
            sock = self._server.sockets[0]
            self.port = int(sock.getsockname()[1])

    async def stop(self) -> None:
        """Disconnect every subscriber and close the server."""
        for sub in list(self._subs):
            if sub.task:
                sub.task.cancel()
            sub.writer.close()
        self._subs.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sub: _Subscriber | None = None
        try:
            while line := await reader.readline():
                try:
                    msg: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    await self._reply(writer, {"reply": "error", "error": "bad json"})
                    continue
                if msg.get("subscribe") and sub is None:
                    sub = _Subscriber(writer, self.queue_size, self.snapshot)
                    sub.offer(self.snapshot())
                    self._subs.add(sub)
                    sub.task = asyncio.create_task(sub.pump())
                elif "cmd" in msg:
                    cmd = str(msg["cmd"])
                    handler = self.handlers.get(cmd)
                    if handler is None:
                        reply: dict[str, Any] = {"reply": cmd, "error": f"unknown command {cmd}"}
                    else:
                        try:
                            reply = {"reply": cmd, **(await handler(dict(msg.get("args", {}))))}
                        except Exception as e:  # a bad command must never take the bus down
                            reply = {"reply": cmd, "error": str(e)}
                    if sub is not None:
                        sub.offer(reply)
                    else:
                        await self._reply(writer, reply)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if sub is not None:
                self._subs.discard(sub)
                if sub.task:
                    sub.task.cancel()
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()


# -- clients ------------------------------------------------------------------------------


async def subscribe(host: str = "127.0.0.1", port: int = 7707) -> AsyncIterator[Event]:
    """Yield events from a running bus, starting with a snapshot."""
    reader, writer = await asyncio.open_connection(host, port, limit=2**22)
    writer.write(b'{"subscribe": true}\n')
    await writer.drain()
    try:
        while line := await reader.readline():
            yield json.loads(line)
    finally:
        writer.close()


async def control(
    cmd: str, args: dict[str, Any] | None = None, host: str = "127.0.0.1", port: int = 7707
) -> dict[str, Any]:
    """Send one control command and return the reply."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write((json.dumps({"cmd": cmd, "args": args or {}}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        return json.loads(line) if line else {"reply": cmd, "error": "no reply"}
    finally:
        writer.close()
