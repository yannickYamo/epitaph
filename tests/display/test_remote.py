"""D3: the remote view (SSH tunnel, reconnect, redraw from a snapshot)."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from epitaph.display import remote
from epitaph.display.app import drive
from epitaph.display.layout import LifeView
from epitaph.display.terminal import TerminalDriver
from epitaph.events import EventBus, make_event

# A stand-in for `ssh -N -L lport:127.0.0.1:rport host`: a local TCP forwarder.
PROXY = r"""
import asyncio, sys
lport, rport = int(sys.argv[1]), int(sys.argv[2])
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data); await w.drain()
    except Exception:
        pass
    finally:
        w.close()
async def handle(r, w):
    try:
        r2, w2 = await asyncio.open_connection("127.0.0.1", rport)
    except OSError:
        w.close(); return
    await asyncio.gather(pipe(r, w2), pipe(r2, w))
async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", lport)
    async with srv:
        await srv.serve_forever()
asyncio.run(main())
"""


def fake_ssh(host: str, lport: int, rport: int) -> list[str]:
    return [sys.executable, "-c", PROXY, str(lport), str(rport)]


def snapshot_of(view: LifeView):
    return lambda: view.snapshot(0.0)


async def wait_for(cond, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise TimeoutError
        await asyncio.sleep(0.01)


def test_tunnel_argv() -> None:
    argv = remote.tunnel_argv("pi", 40001, 7707)
    assert argv[0] == "ssh" and argv[1] == "-N" and argv[-1] == "pi"
    assert "40001:127.0.0.1:7707" in argv
    assert "ExitOnForwardFailure=yes" in argv and "BatchMode=yes" in argv
    assert remote.free_port() > 0


async def test_reconnects_and_redraws_from_snapshot() -> None:
    state = LifeView()
    state.handle(make_event("birth", 3), 0.0)
    state.handle(make_event("word", 3, turn=1, i=0, text="before", char_ms=[0] * 6), 0.0)
    bus = EventBus(port=0, snapshot=snapshot_of(state))
    await bus.start()
    port = bus.port
    got: list[dict[str, Any]] = []
    states: list[bool] = []

    async def connect() -> tuple[str, int]:
        return "127.0.0.1", port

    async def reader() -> None:
        async for e in remote.reconnecting(connect, states.append, backoff=(0.02, 0.1)):
            got.append(e)

    task = asyncio.create_task(reader())
    await wait_for(lambda: bus.subscriber_count == 1)
    bus.publish(make_event("word", 3, turn=1, i=1, text="live"))
    await wait_for(lambda: len(got) == 2)
    assert got[0]["type"] == "snapshot" and got[0]["words"][0]["text"] == "before"
    # the controller goes away and comes back (a restart, a dropped link)
    await bus.stop()
    await wait_for(lambda: states == [True, False], 2)
    state.handle(make_event("word", 3, turn=1, i=1, text="after", char_ms=[0] * 5), 0.0)
    bus2 = EventBus(port=port, snapshot=snapshot_of(state))
    await bus2.start()
    await wait_for(lambda: len(got) >= 3)
    assert got[2]["type"] == "snapshot"
    assert [w["text"] for w in got[2]["words"]] == ["before", "after"]
    assert states[-1] is True
    task.cancel()
    await bus2.stop()


async def test_display_redraws_within_seconds_of_reconnect() -> None:
    """Fault matrix row: kill the tunnel/bus; redraw from a snapshot within 5 s."""
    state = LifeView()
    state.handle(make_event("birth", 1), 0.0)
    state.handle(make_event("word", 1, turn=1, i=0, text="alive", char_ms=[0] * 5), 0.0)
    bus = EventBus(port=0, snapshot=snapshot_of(state))
    await bus.start()
    port = bus.port
    d = TerminalDriver(out=io.StringIO(), size=(40, 8), color="none")

    async def connect() -> tuple[str, int]:
        return "127.0.0.1", port

    def on_state(up: bool) -> None:
        d.view.connected = up

    task = asyncio.create_task(
        drive(d, remote.reconnecting(connect, on_state, (0.05, 0.2)), fps=60, exit_when_done=False)
    )
    await wait_for(lambda: any("alive" in c[0] or c[0] == "a" for c in d.last_cells.values()))
    await bus.stop()
    await wait_for(lambda: not d.view.connected)
    await asyncio.sleep(0.1)
    assert "reconnecting" in (d.last_frame.status or "")  # type: ignore[union-attr]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    bus2 = EventBus(port=port, snapshot=snapshot_of(state))
    await bus2.start()
    await wait_for(lambda: d.view.connected and "reconnecting" not in (d.last_frame.status or ""))  # type: ignore[union-attr]
    assert loop.time() - t0 < 5.0
    text = "".join(ch for (r, c), (ch, _) in sorted(d.last_cells.items()))
    assert "alive" in text
    d.closed = True
    await asyncio.wait_for(task, 2)
    await bus2.stop()


async def test_tunnel_starts_forwards_and_restarts() -> None:
    bus = EventBus(port=0, snapshot=lambda: make_event("snapshot", 9, words=[]))
    await bus.start()
    tunnel = remote.Tunnel("pi", bus.port, argv=fake_ssh, ready_timeout=10)
    try:
        host, lport = await tunnel.ensure()
        assert host == "127.0.0.1" and lport == tunnel.local_port and tunnel.alive
        from epitaph.events import subscribe

        agen = subscribe(host, lport)
        first = await asyncio.wait_for(agen.__anext__(), 5)
        assert first["type"] == "snapshot" and first["life"] == 9
        await agen.aclose()
        # ssh dies: the next ensure() starts it again
        assert tunnel.proc is not None
        tunnel.proc.kill()
        await tunnel.proc.wait()
        assert not tunnel.alive
        await tunnel.ensure()
        assert tunnel.alive and tunnel.starts == 2
    finally:
        await tunnel.close()
        await bus.stop()
    assert not tunnel.alive


async def test_tunnel_reports_ssh_failure() -> None:
    def failing(host: str, lport: int, rport: int) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('Permission denied'); sys.exit(255)",
        ]

    tunnel = remote.Tunnel("pi", 7707, argv=failing)
    with pytest.raises(ConnectionError, match="Permission denied"):
        await tunnel.ensure()


async def test_tunnel_times_out() -> None:
    def silent(host: str, lport: int, rport: int) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    tunnel = remote.Tunnel("pi", 7707, argv=silent, ready_timeout=0.5)
    with pytest.raises(ConnectionError, match="not ready"):
        await tunnel.ensure()
    assert not tunnel.alive


async def test_reconnecting_stops_on_request() -> None:
    stop = asyncio.Event()
    calls = 0

    async def connect() -> tuple[str, int]:
        nonlocal calls
        calls += 1
        stop.set()
        raise OSError("refused")

    got = [e async for e in remote.reconnecting(connect, backoff=(0.01, 0.01), stop=stop)]
    assert got == [] and calls == 1


def test_screen_present(tmp_path: Path) -> None:
    drm = tmp_path / "drm"
    (drm / "card1-HDMI-A-1").mkdir(parents=True)
    (drm / "card1-HDMI-A-1" / "status").write_text("disconnected\n")
    assert not remote.screen_present(drm)
    (drm / "card1-HDMI-A-2").mkdir()
    (drm / "card1-HDMI-A-2" / "status").write_text("connected\n")
    assert remote.screen_present(drm)
    assert not remote.screen_present(tmp_path / "missing")


def test_main_screen_present_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote, "screen_present", lambda: False)
    assert remote.main(["--screen-present"]) == 1
    monkeypatch.setattr(remote, "screen_present", lambda: True)
    assert remote.main(["--screen-present"]) == 0


def test_main_runs_a_terminal_view_against_a_local_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """`epitaph display --port N`: subscribes, draws, and exits cleanly when closed."""
    import threading

    ready = threading.Event()
    port_box: list[int] = []
    stop_box: list[asyncio.AbstractEventLoop] = []

    def serve() -> None:
        async def run() -> None:
            bus = EventBus(
                port=0,
                snapshot=lambda: make_event(
                    "snapshot", 1, words=[{"turn": 1, "i": 0, "text": "hi"}]
                ),
            )
            await bus.start()
            port_box.append(bus.port)
            stop_box.append(asyncio.get_running_loop())
            ready.set()
            await asyncio.sleep(3)
            await bus.stop()

        asyncio.run(run())

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    ready.wait(5)
    created: list[TerminalDriver] = []
    real = remote_make_driver()

    def spy(name: str, cfg: dict[str, Any], **opts: Any) -> Any:
        opts.pop("size", None)
        d = real(
            "terminal",
            cfg,
            out=io.StringIO(),
            size=(30, 6),
            **{k: v for k, v in opts.items() if k not in ("fullscreen",)},
        )
        created.append(d)  # type: ignore[arg-type]

        orig = d.render

        def render(now: float | None = None) -> None:
            orig(now)
            if any(ch == "h" for ch, _ in d.last_cells.values()):  # type: ignore[attr-defined]
                d.closed = True

        d.render = render  # type: ignore[method-assign]
        return d

    import epitaph.display.app as app

    monkeypatch.setattr(app, "make_driver", spy)
    assert remote.main(["--port", str(port_box[0]), "--driver", "terminal", "--fps", "60"]) == 0
    assert created and created[0].closed
    th.join(5)


def remote_make_driver():
    from epitaph.display.app import make_driver

    return make_driver
