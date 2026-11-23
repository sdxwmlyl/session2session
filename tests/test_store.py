"""Tests for the append-only event store (US-001).

The store must work without a QwenPaw host context, so every test pins
``QWENPAW_WORKING_DIR`` to a temp directory and disables the host lookup.
"""

from __future__ import annotations

import errno
import importlib
import json
import logging
import os
import re
import sys
import threading
import types
from pathlib import Path

import pytest

import store

TASK_ID_RE = re.compile(r"^t-\d{8}-\d{4}-[0-9a-f]{8}$")

#: Captured before the autouse fixture replaces the module attribute, so the
#: host-context tests can exercise the real lookup.
REAL_HOST_LOOKUP = store._host_workspace_dir


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Force the store to use a temp workspace, never the real one."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def test_workspace_layout(isolated_workspace):
    tasks = store.tasks_dir()

    assert tasks == isolated_workspace / "session-tasks"
    for name in ("events", "index", "detail"):
        assert (tasks / name).is_dir()
    assert store.events_dir() == tasks / "events"
    assert store.index_dir() == tasks / "index"
    assert store.detail_dir() == tasks / "detail"


def test_workspace_root_prefers_host_context(tmp_path, monkeypatch):
    host = tmp_path / "host-workspace"
    host.mkdir()
    monkeypatch.setattr(store, "_host_workspace_dir", lambda: host)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path / "env-workspace"))

    assert store.workspace_root() == host


def test_workspace_root_uses_host_module(tmp_path, monkeypatch):
    """Main path: ``workspace_root()`` reads ``qwenpaw.config.context``.

    A fake module tree is injected through ``sys.modules`` so the test covers
    the real import branch without depending on QwenPaw being installed.
    """

    host = tmp_path / "host-workspace"
    host.mkdir()

    fake_context = types.ModuleType("qwenpaw.config.context")
    fake_context.get_current_workspace_dir = lambda: str(host)
    fake_config = types.ModuleType("qwenpaw.config")
    fake_config.context = fake_context
    fake_package = types.ModuleType("qwenpaw")
    fake_package.config = fake_config

    monkeypatch.setitem(sys.modules, "qwenpaw", fake_package)
    monkeypatch.setitem(sys.modules, "qwenpaw.config", fake_config)
    monkeypatch.setitem(sys.modules, "qwenpaw.config.context", fake_context)
    # Undo the autouse patch so the real lookup runs.
    monkeypatch.setattr(store, "_host_workspace_dir", REAL_HOST_LOOKUP)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path / "env-workspace"))

    assert store._host_workspace_dir() == host
    assert store.workspace_root() == host
    assert store.tasks_dir() == host / "session-tasks"


def test_workspace_root_falls_back_when_host_returns_none(tmp_path, monkeypatch):
    """Host context present but empty → env var wins."""

    env_workspace = tmp_path / "env-workspace"
    env_workspace.mkdir()

    fake_context = types.ModuleType("qwenpaw.config.context")
    fake_context.get_current_workspace_dir = lambda: None
    fake_config = types.ModuleType("qwenpaw.config")
    fake_config.context = fake_context
    fake_package = types.ModuleType("qwenpaw")
    fake_package.config = fake_config

    monkeypatch.setitem(sys.modules, "qwenpaw", fake_package)
    monkeypatch.setitem(sys.modules, "qwenpaw.config", fake_config)
    monkeypatch.setitem(sys.modules, "qwenpaw.config.context", fake_context)
    monkeypatch.setattr(store, "_host_workspace_dir", REAL_HOST_LOOKUP)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(env_workspace))

    assert store.workspace_root() == env_workspace


def test_workspace_root_falls_back_to_env(isolated_workspace):
    assert store.workspace_root() == isolated_workspace


def test_workspace_root_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.delenv(store.WORKSPACE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert store.workspace_root() == Path.cwd()
    assert (store.tasks_dir() / "events").is_dir()


def test_task_path_is_safe(isolated_workspace):
    path = store.task_path("t-20260101-0000-c3d4e5f6")
    assert path == (
        isolated_workspace / "session-tasks" / "events" / "t-20260101-0000-c3d4e5f6.jsonl"
    )

    for bad in ("", "..", "../escape", "sub/dir", "a b", None, 42):
        with pytest.raises(ValueError):
            store.task_path(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "CON",
        "con",
        "NUL",
        "nul",
        "PRN",
        "aux",
        "COM1",
        "com9",
        "LPT1",
        "lpt9",
        "nul.jsonl",
        "CON.lock",
        "prn.",
        "com1.jsonl",
    ],
)
def test_reserved_device_names_are_rejected(isolated_workspace, bad):
    """``nul.jsonl`` writes to a bit bucket and ``nul.lock`` never unlinks."""

    with pytest.raises(ValueError):
        store.task_path(bad)
    with pytest.raises(ValueError):
        store.append_event(bad, {"type": "created"})

    # Nothing was created for the rejected id.
    events = list(store.events_dir().glob("*"))
    assert events == []


@pytest.mark.parametrize("good", ["console", "null-task", "com0", "lpt10", "aux-task"])
def test_names_near_devices_are_allowed(isolated_workspace, good):
    assert store.task_path(good).name == f"{good}.jsonl"


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #
def test_new_task_id_format_and_uniqueness():
    ids = {store.new_task_id() for _ in range(200)}

    assert len(ids) == 200, "task ids must not collide"
    for task_id in ids:
        assert TASK_ID_RE.match(task_id), task_id
        assert store.task_path(task_id).name == f"{task_id}.jsonl"


def test_new_task_ids_are_unique_over_5000_draws():
    """4 hex digits collided in ~27% of 30x200 batches; 8 must not."""

    ids = [store.new_task_id() for _ in range(5000)]

    assert len(set(ids)) == 5000
    assert all(TASK_ID_RE.match(task_id) for task_id in ids)

    # Two tasks with distinct ids never share an event log.
    first, second = ids[0], ids[1]
    store.append_event(first, {"type": "created", "owner": "A"})
    store.append_event(second, {"type": "created", "owner": "B"})
    assert [event["owner"] for event in store.read_events(first)] == ["A"]
    assert [event["owner"] for event in store.read_events(second)] == ["B"]


# --------------------------------------------------------------------------- #
# Append / read
# --------------------------------------------------------------------------- #
def test_append_and_read_events():
    task_id = store.new_task_id()

    first = store.append_event(task_id, {"type": "created", "title": "整理标书"})
    second = store.append_event(task_id, {"type": "working"})
    third = store.append_event(task_id, {"type": "completed", "note": "完成"})

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert third["seq"] == 3
    assert all("at" in event and "T" in event["at"] for event in (first, second, third))

    events = store.read_events(task_id)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert [event["type"] for event in events] == ["created", "working", "completed"]
    assert events[2]["note"] == "完成"  # UTF-8, not escaped on disk

    # One line per event, valid JSONL.
    raw = store.task_path(task_id).read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 3
    assert [json.loads(line)["seq"] for line in lines] == [1, 2, 3]


def test_append_event_is_caller_seq_proof():
    task_id = store.new_task_id()

    stored = store.append_event(task_id, {"type": "created", "seq": 99})

    assert stored["seq"] == 1
    assert store.read_events(task_id)[0]["seq"] == 1


def test_append_event_rejects_non_dict():
    with pytest.raises(TypeError):
        store.append_event(store.new_task_id(), ["not", "a", "dict"])


def test_append_event_rejects_non_finite_numbers():
    """Refuse to write what the reader would have to skip."""

    task_id = store.new_task_id()
    with pytest.raises(ValueError):
        store.append_event(task_id, {"type": "pulse", "value": float("nan")})

    assert store.read_events(task_id) == []


def test_read_missing_task_returns_empty():
    assert store.read_events("t-20260101-0000-d4e5f6a7") == []


def test_fields_survive_module_reload(isolated_workspace):
    """Simulates a restart: a freshly loaded module sees what is on disk."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created", "payload": {"n": 1}})

    reloaded = importlib.reload(store)
    reloaded._host_workspace_dir = lambda: None
    events = reloaded.read_events(task_id)

    assert len(events) == 1
    assert events[0]["payload"] == {"n": 1}
    assert reloaded.append_event(task_id, {"type": "working"})["seq"] == 2


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #
def test_acquire_lock_retries_transient_permission_error(monkeypatch):
    """Windows reports EACCES while another writer is unlinking the lock."""

    task_id = store.new_task_id()
    real_open = os.open
    lock_attempts = {"n": 0}

    def flaky_open(path, flags, mode=0o777):
        if str(path).endswith(".lock"):
            lock_attempts["n"] += 1
            if lock_attempts["n"] <= 3:
                raise PermissionError(errno.EACCES, "in use (delete pending)", str(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", flaky_open)

    stored = store.append_event(task_id, {"type": "created"})

    assert stored["seq"] == 1
    assert lock_attempts["n"] == 4, "three EACCES failures must be retried"
    assert [event["seq"] for event in store.read_events(task_id)] == [1]


@pytest.mark.parametrize(
    "exc",
    [
        OSError(errno.EPERM, "operation not permitted"),
        OSError(errno.EBUSY, "resource busy"),
        OSError(None, "sharing violation", None, 32),
    ],
)
def test_acquire_lock_retries_other_transient_oserrors(monkeypatch, exc):
    task_id = store.new_task_id()
    real_open = os.open
    lock_attempts = {"n": 0}

    def flaky_open(path, flags, mode=0o777):
        if str(path).endswith(".lock"):
            lock_attempts["n"] += 1
            if lock_attempts["n"] == 1:
                raise exc
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", flaky_open)

    assert store.append_event(task_id, {"type": "created"})["seq"] == 1
    assert lock_attempts["n"] == 2


def test_acquire_lock_propagates_non_transient_errors(monkeypatch):
    task_id = store.new_task_id()

    def broken_open(path, flags, mode=0o777):
        raise FileNotFoundError(errno.ENOENT, "no such file", str(path))

    monkeypatch.setattr(os, "open", broken_open)

    with pytest.raises(FileNotFoundError):
        store.append_event(task_id, {"type": "created"})


def test_acquire_lock_times_out_and_leaves_data_untouched(monkeypatch):
    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    # A live (non-stale) lock file blocks the writer.
    lock_path = store._lock_dir() / f"{task_id}.lock"
    lock_path.write_text("someone-else\n", encoding="ascii")

    monkeypatch.setattr(store, "LOCK_RETRIES", 3)
    monkeypatch.setattr(store, "LOCK_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(store, "LOCK_SLEEP_JITTER_SECONDS", 0.0)

    with pytest.raises(TimeoutError):
        store.append_event(task_id, {"type": "working"})

    assert [event["seq"] for event in store.read_events(task_id)] == [1]
    assert lock_path.exists(), "a foreign lock must not be stolen"


def test_stale_lock_is_reclaimed(isolated_workspace, monkeypatch):
    task_id = store.new_task_id()
    lock_path = store._lock_dir() / f"{task_id}.lock"
    lock_path.write_text("dead-writer\n", encoding="ascii")
    stale_mtime = lock_path.stat().st_mtime - (store.LOCK_STALE_SECONDS + 5)
    os.utime(lock_path, (stale_mtime, stale_mtime))

    assert store.append_event(task_id, {"type": "created"})["seq"] == 1
    assert not lock_path.exists()


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_writers_keep_seq_unique_and_contiguous():
    task_id = store.new_task_id()
    per_thread = 50
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def writer(worker: int) -> None:
        try:
            start.wait(timeout=5)
            for i in range(per_thread):
                store.append_event(
                    task_id, {"type": "pulse", "worker": worker, "i": i}
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)

    events = store.read_events(task_id)
    seqs = [event["seq"] for event in events]

    assert len(events) == 2 * per_thread
    assert len(set(seqs)) == len(seqs), "seq must be unique"
    assert seqs == list(range(1, 2 * per_thread + 1)), "seq must be 1..N in order"

    worker_counts = {}
    for event in events:
        worker_counts[event["worker"]] = worker_counts.get(event["worker"], 0) + 1
    assert worker_counts == {1: per_thread, 2: per_thread}


@pytest.mark.parametrize("round_index", range(5))
def test_concurrent_writers_8_threads_lose_no_events(round_index):
    """The failure mode was 15-50% data loss: EACCES was never retried."""

    task_id = store.new_task_id()
    thread_count = 8
    per_thread = 25
    total = thread_count * per_thread
    errors: list[BaseException] = []
    start = threading.Barrier(thread_count)

    def writer(worker: int) -> None:
        try:
            start.wait(timeout=10)
            for i in range(per_thread):
                store.append_event(
                    task_id, {"type": "pulse", "worker": worker, "i": i}
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)

    events = store.read_events(task_id)
    seqs = [event["seq"] for event in events]

    assert len(events) == total, f"round {round_index}: lost events"
    assert len(set(seqs)) == total, "seq must be unique"
    assert seqs == list(range(1, total + 1)), "seq must be 1..N in order"

    worker_counts: dict[int, int] = {}
    for event in events:
        worker_counts[event["worker"]] = worker_counts.get(event["worker"], 0) + 1
    assert worker_counts == {worker: per_thread for worker in range(thread_count)}


# --------------------------------------------------------------------------- #
# Corruption tolerance
# --------------------------------------------------------------------------- #
def test_corrupt_lines_are_skipped_and_logged(caplog):
    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})
    store.append_event(task_id, {"type": "working"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{not json at all\n")
        handle.write('{"type": "truncated"\n')
        handle.write("[1, 2, 3]\n")
        handle.write("\n")

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        events = store.read_events(task_id)

    assert [event["type"] for event in events] == ["created", "working"]
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 3, [rec.getMessage() for rec in warnings]
    assert all("skipping" in rec.getMessage() for rec in warnings)

    # The log stays writable and seq continues after the last valid record.
    appended = store.append_event(task_id, {"type": "completed"})
    assert appended["seq"] == 3
    assert [event["seq"] for event in store.read_events(task_id)] == [1, 2, 3]


def test_nan_and_infinity_lines_are_rejected(caplog):
    """``{"x":NaN}`` used to parse into a seq-less dict and inflate ``seq``."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"x": NaN}\n')
        handle.write('{"x": Infinity}\n')
        handle.write('{"x": -Infinity}\n')

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        events = store.read_events(task_id)

    assert [event["type"] for event in events] == ["created"]
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 3, [rec.getMessage() for rec in warnings]
    assert all("skipping corrupt event line" in rec.getMessage() for rec in warnings)

    # seq is not inflated by the skipped lines.
    assert store.append_event(task_id, {"type": "working"})["seq"] == 2


def test_overflowing_json_numbers_are_skipped_and_logged(caplog):
    """``1e309`` is *valid* JSON: ``json.loads`` overflows it to ``inf``.

    ``parse_constant`` never fires for it, so the record must be rejected by
    inspecting the parsed value instead. An ``inf`` in the list used to open a
    hole: ``len(events) + 1`` gave 3 while seq 2 was still free.
    """

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"n": 1e309}\n')
        handle.write('{"n": -1e309}\n')

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        events = store.read_events(task_id)

    assert [event["type"] for event in events] == ["created"]
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 2, [rec.getMessage() for rec in warnings]
    assert all("skipping" in rec.getMessage() for rec in warnings)
    assert all("non-finite" in rec.getMessage() for rec in warnings)

    # The next seq continues right after the last valid record, no gap.
    appended = store.append_event(task_id, {"type": "working"})
    assert appended["seq"] == 2
    assert [event["seq"] for event in store.read_events(task_id)] == [1, 2]


def test_nested_non_finite_value_is_skipped(caplog):
    """The check must reach into dict/list nesting, not just the top level."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"a": {"b": [1e309]}}\n')

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        events = store.read_events(task_id)

    assert [event["type"] for event in events] == ["created"]
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1, [rec.getMessage() for rec in warnings]
    assert "non-finite" in warnings[0].getMessage()

    assert store.append_event(task_id, {"type": "working"})["seq"] == 2


def test_append_seq_continues_after_records_without_integer_seq(caplog):
    """A legacy record (old format, no ``seq``) must not shift the counter.

    ``len(events) + 1`` counted such records, so one import of an old file
    gave every later append a hole. Only real ``int`` seqs may count.
    """

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type": "legacy", "note": "old format"}\n')  # no seq
        handle.write('{"type": "odd", "seq": "7"}\n')  # seq is not an int
        handle.write('{"type": "worse", "seq": true}\n')  # bool is not a seq

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        events = store.read_events(task_id)
    assert [rec for rec in caplog.records if rec.levelno == logging.WARNING] == []

    assert [event.get("seq") for event in events] == [1, None, "7", True]

    stored = store.append_event(task_id, {"type": "working"})
    assert stored["seq"] == 2
    assert [event.get("seq") for event in store.read_events(task_id)] == [
        1,
        None,
        "7",
        True,
        2,
    ]


def test_nested_valid_payload_is_kept():
    """Guard against over-eager rejection: finite numbers at depth stay."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "pulse", "data": {"a": [1.5, 1e308]}})

    events = store.read_events(task_id)
    assert len(events) == 1
    assert events[0]["data"] == {"a": [1.5, 1e308]}


def test_truncated_tail_without_newline_is_tolerated():
    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "wor')  # crash mid-write, no trailing newline

    assert len(store.read_events(task_id)) == 1


def test_append_heals_torn_tail_instead_of_swallowing_the_event(caplog):
    """A half-written tail used to eat the next event and repeat its seq."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    path = store.task_path(task_id)
    with open(path, "ab") as handle:
        handle.write(b'{"type": "wor')  # no trailing newline

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        stored = store.append_event(task_id, {"type": "working"})

    assert stored["seq"] == 2

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert b'{"type": "wor\n' in raw, "the torn line must be terminated first"
    assert len([line for line in raw.split(b"\n") if line.strip()]) == 3

    events = store.read_events(task_id)
    assert [event["seq"] for event in events] == [1, 2]
    assert [event["type"] for event in events] == ["created", "working"]

    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("skipping corrupt event line" in message for message in warnings)
    assert any("healing torn tail" in message for message in warnings)

    # And the file stays healthy afterwards: no repeated seq.
    assert store.append_event(task_id, {"type": "completed"})["seq"] == 3
    assert [event["seq"] for event in store.read_events(task_id)] == [1, 2, 3]
