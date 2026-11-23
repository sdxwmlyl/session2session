"""Tests for the materialised views (US-002).

Covers replay of a full lifecycle, tolerance of bad events (one illegal
transition must not blank the view), the fixed key set, ``history`` ordering,
``index_for_session`` filtering / ordering / the 50-entry cap, and
idempotent appends.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

import store
import views
from models import ACTIVE_STATES
from views import VIEW_KEYS, index_for_session, iter_session_views, materialize


@pytest.fixture()
def isolated_workspace(tmp_path, monkeypatch):
    """Point the store at a temp workspace, never at the real one."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


def _new_task(**created):
    """Create a task with a ``created`` event and return ``(task_id, base_at)``."""

    task_id = store.new_task_id()
    event = {"type": "created", "at": "2026-11-22T10:00:00+08:00"}
    event.update(created)
    store.append_event(task_id, event)
    return task_id


def _at(seconds: int) -> str:
    """A monotonically increasing timestamp (base 10:00:00)."""

    minutes, sec = divmod(seconds, 60)
    return f"2026-11-22T10:{minutes:02d}:{sec:02d}+08:00"


# --------------------------------------------------------------------------- #
# materialize: shape
# --------------------------------------------------------------------------- #
def test_materialize_returns_the_fixed_key_set_for_unknown_task(isolated_workspace):
    view = materialize("t-20260101-0000-d4e5f6a7")

    assert tuple(view) == VIEW_KEYS
    assert set(view) == {
        "task_id",
        "state",
        "title",
        "goal",
        "from_session",
        "to_session",
        "summary",
        "artifacts",
        "history",
        "updated_at",
    }
    assert view["state"] == "submitted"
    assert view["history"] == []
    assert view["artifacts"] == []
    assert view["updated_at"] is None


def test_full_lifecycle_replays_to_the_expected_view(isolated_workspace):
    task_id = _new_task(
        title="整理标书",
        goal="把 3 份标书合并成 1 份",
        from_session="console:团子",
        to_session="console:worker",
    )
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(1)})
    store.append_event(
        task_id, {"type": "state", "value": "input-required", "at": _at(2)}
    )
    store.append_event(
        task_id, {"type": "summary", "value": "等对方给解压密码", "at": _at(3)}
    )
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(4)})
    store.append_event(
        task_id, {"type": "artifacts", "value": ["out/merged.docx"], "at": _at(5)}
    )
    store.append_event(
        task_id,
        {"type": "artifacts", "value": ["out/a.docx", {"path": "out/c.pdf"}], "at": _at(6)},
    )
    store.append_event(
        task_id,
        {"type": "state", "value": "completed", "at": _at(7), "summary": "做完了"},
    )

    view = materialize(task_id)

    assert tuple(view) == VIEW_KEYS
    assert view["task_id"] == task_id
    assert view["state"] == "completed"
    assert view["title"] == "整理标书"
    assert view["goal"] == "把 3 份标书合并成 1 份"
    assert view["from_session"] == "console:团子"
    assert view["to_session"] == "console:worker"
    # The latest summary event wins; the ``summary`` key on the final state
    # event is not a summary event and must not overwrite it.
    assert view["summary"] == "等对方给解压密码"
    # ``artifacts`` is replaced by the latest artifacts event, not appended to.
    assert view["artifacts"] == ["out/a.docx", {"path": "out/c.pdf"}]
    assert view["updated_at"] == _at(7)


def test_history_is_the_compact_event_sequence_in_file_order(isolated_workspace):
    task_id = _new_task(title="T", goal="G")
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(1)})
    store.append_event(task_id, {"type": "pulse", "note": "还在跑", "at": _at(2)})
    store.append_event(
        task_id, {"type": "summary", "value": "半成品", "at": _at(3)}
    )

    history = materialize(task_id)["history"]

    assert [entry["type"] for entry in history] == [
        "created",
        "state",
        "pulse",
        "summary",
    ]
    assert [entry["seq"] for entry in history] == [1, 2, 3, 4]
    assert [entry["at"] for entry in history] == [
        "2026-11-22T10:00:00+08:00",
        _at(1),
        _at(2),
        _at(3),
    ]
    assert all(set(entry) == {"type", "value", "at", "seq"} for entry in history)
    assert history[1]["value"] == "working"
    assert history[3]["value"] == "半成品"
    assert history[0]["value"] is None  # created carries no ``value`` field


def test_summary_and_artifacts_accept_the_type_named_field(isolated_workspace):
    task_id = _new_task()
    store.append_event(task_id, {"type": "summary", "summary": "简述"})
    store.append_event(task_id, {"type": "artifacts", "artifacts": "out/one.docx"})

    view = materialize(task_id)

    assert view["summary"] == "简述"
    assert view["artifacts"] == ["out/one.docx"]


# --------------------------------------------------------------------------- #
# materialize: tolerance
# --------------------------------------------------------------------------- #
def test_illegal_state_event_is_skipped_with_a_warning(isolated_workspace, caplog):
    """``completed -> working`` written straight to the log (bypassing the
    validator) must not crash the view nor reopen the task."""

    task_id = _new_task()
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(1)})
    store.append_event(task_id, {"type": "state", "value": "completed", "at": _at(2)})
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(3)})

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        view = materialize(task_id)

    assert view["state"] == "completed"
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "skipping state event" in warnings[0]
    assert "completed->working" in warnings[0]

    # The bad attempt is still visible in the log view, only its effect is not.
    assert [entry["value"] for entry in view["history"]][-1] == "working"


def test_unknown_state_value_and_non_string_value_are_skipped(
    isolated_workspace, caplog
):
    task_id = _new_task()
    store.append_event(task_id, {"type": "state", "value": "working", "at": _at(1)})
    store.append_event(task_id, {"type": "state", "value": "done", "at": _at(2)})
    store.append_event(task_id, {"type": "state", "value": 7, "at": _at(3)})
    store.append_event(task_id, {"type": "state", "at": _at(4)})  # no value at all
    store.append_event(task_id, {"type": "state", "value": "completed", "at": _at(5)})

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        view = materialize(task_id)

    assert view["state"] == "completed"
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 3, warnings
    assert all("skipping state event" in message for message in warnings)


def test_corrupt_line_does_not_break_the_view(isolated_workspace):
    task_id = _new_task(title="T")
    store.append_event(task_id, {"type": "state", "value": "working"})

    with open(store.task_path(task_id), "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type": "state", "value": "comple\n')  # truncated write

    view = materialize(task_id)

    assert view["state"] == "working"
    assert view["title"] == "T"
    assert len(view["history"]) == 2


def test_unknown_event_types_only_land_in_history(isolated_workspace):
    task_id = _new_task(title="T")
    store.append_event(task_id, {"type": "nudge", "note": "催一下"})
    store.append_event(task_id, {"type": "pulse", "note": "还在跑"})

    view = materialize(task_id)

    assert view["state"] == "submitted"
    assert view["title"] == "T"
    assert view["summary"] is None
    assert [entry["type"] for entry in view["history"]] == [
        "created",
        "nudge",
        "pulse",
    ]


def test_state_named_event_type_is_honoured(isolated_workspace):
    """Later stories log ``{"type": "completed"}``; treat it as a transition."""

    task_id = _new_task()
    store.append_event(task_id, {"type": "working", "at": _at(1)})
    store.append_event(task_id, {"type": "completed", "summary": "done", "at": _at(2)})

    view = materialize(task_id)

    assert view["state"] == "completed"
    assert [entry["type"] for entry in view["history"]] == [
        "created",
        "working",
        "completed",
    ]


# --------------------------------------------------------------------------- #
# index_for_session
# --------------------------------------------------------------------------- #
def _task_from_to(session_from: str, session_to: str, title: str, at: str) -> str:
    task_id = store.new_task_id()
    store.append_event(
        task_id,
        {
            "type": "created",
            "title": title,
            "from_session": session_from,
            "to_session": session_to,
            "at": at,
        },
    )
    store.append_event(task_id, {"type": "state", "value": "working", "at": at})
    return task_id


def test_index_filters_by_role(isolated_workspace):
    sent = _task_from_to("console:me", "console:worker", "我发起的", _at(1))
    received = _task_from_to("console:boss", "console:me", "我要办的", _at(2))
    _task_from_to("console:other", "console:worker", "跟我无关", _at(3))

    both = index_for_session("console:me")
    as_from = index_for_session("console:me", "from")
    as_to = index_for_session("console:me", "to")

    assert [entry["task_id"] for entry in both] == [received, sent]
    assert [entry["task_id"] for entry in as_from] == [sent]
    assert [entry["task_id"] for entry in as_to] == [received]


def test_index_entries_are_summaries_only(isolated_workspace):
    _task_from_to("console:me", "console:worker", "标题", _at(1))

    entry = index_for_session("console:me", "from")[0]

    assert set(entry) == {"task_id", "title", "state", "from", "to", "updated_at"}
    assert entry["title"] == "标题"
    assert entry["state"] == "working"
    assert entry["from"] == "console:me"
    assert entry["to"] == "console:worker"
    assert entry["updated_at"] == _at(1)


def test_index_sorts_by_updated_at_descending(isolated_workspace):
    oldest = _task_from_to("console:me", "console:w", "oldest", _at(1))
    newest = _task_from_to("console:me", "console:w", "newest", _at(9))
    middle = _task_from_to("console:me", "console:w", "middle", _at(5))
    # A later event (not a later creation) moves a task to the top.
    store.append_event(middle, {"type": "pulse", "at": _at(30)})

    order = [entry["task_id"] for entry in index_for_session("console:me", "from")]

    assert order == [middle, newest, oldest]


def test_index_ties_are_broken_by_task_id_for_a_stable_order(isolated_workspace):
    """``at`` has second resolution; same-second tasks must not shuffle."""

    ids = [_task_from_to("console:me", "console:w", "t", _at(1)) for _ in range(4)]

    first = [entry["task_id"] for entry in index_for_session("console:me", "from")]
    second = [entry["task_id"] for entry in index_for_session("console:me", "from")]

    assert first == second
    assert first == sorted(ids, reverse=True)


def test_index_caps_at_fifty_entries_newest_first(isolated_workspace):
    created = [
        _task_from_to("console:me", "console:w", f"task-{i:02d}", _at(i))
        for i in range(55)
    ]

    entries = index_for_session("console:me", "from")

    assert len(entries) == views.SUMMARY_LIMIT == 50
    assert entries[0]["task_id"] == created[-1]
    assert [entry["updated_at"] for entry in entries] == sorted(
        [entry["updated_at"] for entry in entries], reverse=True
    )
    # The five oldest fell off the end.
    dropped = set(created[:5])
    assert dropped.isdisjoint({entry["task_id"] for entry in entries})


def test_index_limit_parameter_never_exceeds_fifty(isolated_workspace):
    for i in range(3):
        _task_from_to("console:me", "console:w", f"t{i}", _at(i))

    assert len(index_for_session("console:me", "from", limit=2)) == 2
    assert index_for_session("console:me", "from", limit=0) == []
    assert index_for_session("console:me", "from", limit=-1) == []
    assert len(index_for_session("console:me", "from", limit=500)) == 3


def test_index_with_no_matching_tasks_is_empty(isolated_workspace):
    _task_from_to("console:a", "console:b", "t", _at(1))

    assert index_for_session("console:nobody") == []


@pytest.mark.parametrize("role", ["sender", "FROM", "", None])
def test_index_rejects_unknown_role(isolated_workspace, role):
    with pytest.raises(ValueError):
        index_for_session("console:me", role)


def test_index_rejects_empty_session_id(isolated_workspace):
    for bad in ("", None, 42):
        with pytest.raises(ValueError):
            index_for_session(bad)


def test_index_skips_unreadable_task_files(isolated_workspace, caplog):
    good = _task_from_to("console:me", "console:w", "good", _at(1))
    # A file name task_path() refuses: the index must skip it, not raise.
    (store.events_dir() / "bad name.jsonl").write_text(
        json.dumps({"type": "created", "from_session": "console:me"}) + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        entries = index_for_session("console:me")

    assert [entry["task_id"] for entry in entries] == [good]


# --------------------------------------------------------------------------- #
# iter_session_views: filter first, sort, cap last
# --------------------------------------------------------------------------- #
def _finish(task_id: str, at: str) -> str:
    store.append_event(task_id, {"type": "state", "value": "completed", "at": at})
    return task_id


def test_iter_session_views_filters_by_state_before_the_limit(isolated_workspace):
    """The whole point of the helper: an old match must survive the cap."""

    active = [
        _task_from_to("console:me", "console:w", f"active-{i}", _at(i))
        for i in range(3)
    ]
    for index in range(views.SUMMARY_LIMIT + 10):
        _finish(
            _task_from_to("console:me", "console:w", f"done-{index}", _at(100 + index)),
            _at(100 + index),
        )

    rows = iter_session_views("console:me", "all", states=ACTIVE_STATES, limit=5)

    assert [row["state"] for row in rows] == ["working"] * 3
    assert {row["task_id"] for row in rows} == set(active)


def test_iter_session_views_keeps_newest_first_and_honours_the_limit(
    isolated_workspace,
):
    old = _task_from_to("console:me", "console:w", "old", _at(1))
    new = _task_from_to("console:me", "console:w", "new", _at(9))
    middle = _task_from_to("console:me", "console:w", "middle", _at(5))

    rows = iter_session_views("console:me", "from")

    assert [row["task_id"] for row in rows] == [new, middle, old]
    assert [
        row["task_id"] for row in iter_session_views("console:me", "from", limit=2)
    ] == [new, middle]
    assert iter_session_views("console:me", "from", limit=0) == []


def test_iter_session_views_rows_are_summaries_plus_the_last_pulse(
    isolated_workspace,
):
    task_id = _task_from_to("console:me", "console:w", "有心跳", _at(1))
    store.append_event(task_id, {"type": "pulse", "note": "第一拍", "at": _at(2)})
    store.append_event(task_id, {"type": "pulse", "note": "第二拍", "at": _at(7)})

    row = iter_session_views("console:me", "from")[0]

    assert set(row) == set(views.SUMMARY_KEYS) | {views.PULSE_KEY}
    assert row["last_pulse_at"] == _at(7)
    assert row["title"] == "有心跳"
    assert row["from"] == "console:me"
    assert row["to"] == "console:w"


def test_iter_session_views_says_none_when_never_pulsed(isolated_workspace):
    _task_from_to("console:me", "console:w", "没打点", _at(1))

    assert iter_session_views("console:me", "from")[0]["last_pulse_at"] is None


def test_iter_session_views_filters_by_role(isolated_workspace):
    sent = _task_from_to("console:me", "console:w", "派出", _at(1))
    received = _task_from_to("console:boss", "console:me", "收到", _at(2))

    assert [row["task_id"] for row in iter_session_views("console:me", "from")] == [sent]
    assert [row["task_id"] for row in iter_session_views("console:me", "to")] == [
        received
    ]
    assert {row["task_id"] for row in iter_session_views("console:me")} == {
        sent,
        received,
    }


@pytest.mark.parametrize("states", [("done",), "working"])
def test_iter_session_views_rejects_an_unknown_state(isolated_workspace, states):
    with pytest.raises(ValueError):
        iter_session_views("console:me", states=states)


@pytest.mark.parametrize("session_id,role", [("", "all"), ("console:me", "sender")])
def test_iter_session_views_validates_its_query(isolated_workspace, session_id, role):
    with pytest.raises(ValueError):
        iter_session_views(session_id, role)


def test_iter_session_views_rejects_a_bad_limit(isolated_workspace):
    for bad in (-1, "5", True):
        with pytest.raises(ValueError):
            iter_session_views("console:me", limit=bad)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_append_event_once_ignores_a_repeated_idempotency_key(isolated_workspace):
    task_id = _new_task()
    event = {
        "type": "state",
        "value": "working",
        "idempotency_key": "report-1",
        "at": _at(1),
    }

    first = views.append_event_once(task_id, event)
    second = views.append_event_once(task_id, dict(event))

    assert first["seq"] == second["seq"] == 2
    assert len(store.read_events(task_id)) == 2
    assert [e["type"] for e in store.read_events(task_id)] == ["created", "state"]


def test_append_event_once_keeps_distinct_keys(isolated_workspace):
    task_id = _new_task()

    views.append_event_once(task_id, {"type": "summary", "value": "a", "idempotency_key": "k1"})
    views.append_event_once(task_id, {"type": "summary", "value": "b", "idempotency_key": "k2"})

    assert len(store.read_events(task_id)) == 3
    assert materialize(task_id)["summary"] == "b"


def test_append_event_once_without_key_appends_every_time(isolated_workspace):
    task_id = _new_task()

    views.append_event_once(task_id, {"type": "pulse"})
    views.append_event_once(task_id, {"type": "pulse"})

    assert len(store.read_events(task_id)) == 3


def test_append_event_once_is_idempotent_across_threads(isolated_workspace):
    task_id = _new_task()
    event = {"type": "summary", "value": "只此一份", "idempotency_key": "race"}
    results: list[dict] = []
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def writer() -> None:
        try:
            start.wait(timeout=5)
            results.append(views.append_event_once(task_id, dict(event)))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len({record["seq"] for record in results}) == 1, "all callers see one record"
    assert len(store.read_events(task_id)) == 2


def test_append_event_once_rejects_non_dict(isolated_workspace):
    with pytest.raises(TypeError):
        views.append_event_once(store.new_task_id(), ["nope"])


# --------------------------------------------------------------------------- #
# append_state: idempotent retry + per-task serialisation
# --------------------------------------------------------------------------- #
def test_append_state_retry_with_the_same_key_returns_the_first_event(
    isolated_workspace,
):
    """A retried terminal transition must not raise ``completed->completed``."""

    task_id = _new_task()
    views.append_state(task_id, "working")
    before = len(store.read_events(task_id))

    first = views.append_state(task_id, "completed", idempotency_key="k")
    second = views.append_state(task_id, "completed", idempotency_key="k")

    assert second == first
    assert second["seq"] == first["seq"]
    assert second["value"] == "completed"
    assert len(store.read_events(task_id)) == before + 1
    assert materialize(task_id)["state"] == "completed"


def test_append_state_retry_with_a_key_skips_validation_entirely(
    isolated_workspace,
):
    """The key is checked before the transition, so a retry is not re-judged.

    Here the retry even asks for a *different* target: the key already exists,
    so the recorded event comes back untouched and nothing is written. That is
    the contract — "did my earlier call land?" must be answerable without
    re-deriving whether the target is still reachable.
    """

    task_id = _new_task()
    views.append_state(task_id, "working")
    first = views.append_state(task_id, "completed", idempotency_key="k")
    after_first = len(store.read_events(task_id))

    second = views.append_state(task_id, "failed", idempotency_key="k")

    assert second == first
    assert second["value"] == "completed"
    assert len(store.read_events(task_id)) == after_first
    assert materialize(task_id)["state"] == "completed"


def test_append_state_concurrent_terminal_transitions_accept_exactly_one(
    isolated_workspace,
):
    """8 threads, 4x ``completed`` and 4x ``failed``: one wins, seven raise.

    Without the per-task lock every thread validates against the same
    ``working`` view, all eight append, and replay keeps whichever terminal
    event came second — a success returned to a caller whose event was then
    silently discarded. 20 rounds, because one lucky interleaving proves
    nothing.
    """

    base_order = ["completed"] * 4 + ["failed"] * 4

    for round_index in range(20):
        task_id = _new_task(title=f"race-{round_index}")
        views.append_state(task_id, "working")

        # Alternating the start order keeps both outcomes covered: which
        # target wins depends on the interpreter's lock wake-up order, and a
        # test that only ever saw one of them win would miss half the space.
        order = list(base_order)
        if round_index % 2:
            order.reverse()

        start = threading.Barrier(len(order))
        accepted: list[tuple[str, dict]] = []
        rejected: list[tuple[str, BaseException]] = []
        collect = threading.Lock()

        def writer(target: str) -> None:
            try:
                start.wait(timeout=30)
                record = views.append_state(task_id, target)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                with collect:
                    rejected.append((target, exc))
            else:
                with collect:
                    accepted.append((target, record))

        threads = [
            threading.Thread(target=writer, args=(target,)) for target in order
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert not any(thread.is_alive() for thread in threads), "a writer hung"

        # Exactly one success, and every loser got a real ValueError.
        assert len(accepted) == 1, f"round {round_index}: {accepted} {rejected}"
        assert len(rejected) == len(order) - 1, f"round {round_index}"
        for _target, error in rejected:
            assert isinstance(error, ValueError), f"round {round_index}: {error!r}"
            assert "illegal transition" in str(error), f"round {round_index}: {error}"

        winner = accepted[0][0]
        view = materialize(task_id)
        assert view["state"] == winner, f"round {round_index}"

        # The log itself must hold exactly one accepted terminal state event,
        # and it must be the winner's: no "both were accepted" log.
        terminal = [
            event["value"]
            for event in store.read_events(task_id)
            if event.get("type") == "state"
            and event.get("value") in ("completed", "failed")
        ]
        assert terminal == [winner], f"round {round_index}: {terminal}"

        # Replay agrees with the winning caller, so nobody was told "done"
        # about an event that a later replay throws away.
        replayed = [
            entry["value"] for entry in view["history"] if entry["type"] == "state"
        ]
        assert replayed == ["working", winner], f"round {round_index}: {replayed}"
