from __future__ import annotations

import multiprocessing as mp

import pytest

from epitaph.state import (
    AlreadyRunning,
    InstanceLock,
    LifeCounter,
    atomic_write,
    life_dir,
    unfinished_lives,
)


def test_atomic_write_replaces(state_dir) -> None:
    p = state_dir / "x"
    atomic_write(p, "one")
    atomic_write(p, "two")
    assert p.read_text() == "two"
    assert not [f for f in state_dir.iterdir() if f.name.startswith(".")]


def test_life_counter_persists(state_dir) -> None:
    c = LifeCounter(state_dir)
    assert c.current() == 0
    assert c.next() == 1 and c.next() == 2
    assert LifeCounter(state_dir).current() == 2


def _try_lock(path, q) -> None:
    try:
        InstanceLock(path).acquire()
        q.put("acquired")
    except AlreadyRunning as e:
        q.put(str(e))


def test_single_instance_lock(state_dir) -> None:
    with InstanceLock(state_dir):
        q: mp.Queue[str] = mp.Queue()
        p = mp.Process(target=_try_lock, args=(state_dir, q))
        p.start()
        p.join()
        assert "epitaph ctl new-life" in q.get()
    InstanceLock(state_dir).acquire()


def test_unfinished_lives(state_dir) -> None:
    a, b = life_dir(state_dir, 1), life_dir(state_dir, 2)
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("")
    (a / "death.json").write_text("{}")
    assert unfinished_lives(state_dir) == [b]


def test_counter_ignores_garbage(state_dir) -> None:
    (state_dir / "life_counter").write_text("garbage")
    assert LifeCounter(state_dir).current() == 0
    with pytest.raises(ValueError):
        int("garbage")


def test_status_and_lock_release(state_dir) -> None:
    import json

    from epitaph.state import atomic_write_json, write_status

    write_status(state_dir, {"life": 3, "phase": "birth"})
    assert json.loads((state_dir / "status.json").read_text())["life"] == 3
    atomic_write_json(state_dir / "nested" / "a.json", [1, 2])
    assert (state_dir / "nested" / "a.json").exists()
    lock = InstanceLock(state_dir)
    lock.acquire()
    lock.release()
    lock.release()  # idempotent
    assert unfinished_lives(state_dir / "missing") == []
