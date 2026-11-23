"""Tests for the task state machine (US-002).

The transition table is asserted twice: once by hard-coding the expected
edges (so an accidental table edit fails here) and once by exercising every
edge and every non-edge through :func:`models.validate_transition`.

The "raises and writes nothing" criterion is checked against the real event
log: the log file bytes must be identical before and after the rejected call.
"""

from __future__ import annotations

import itertools
import logging

import pytest

import models
import store
import views
from models import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    ALL_STATES,
    TERMINAL_STATES,
    TaskState,
    validate_transition,
)


@pytest.fixture()
def isolated_workspace(tmp_path, monkeypatch):
    """Point the store at a temp workspace, never at the real one."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


#: The expected table, written out by hand. Duplicated on purpose: a test
#: that derives its expectations from ``ALLOWED_TRANSITIONS`` cannot detect a
#: wrong table.
EXPECTED_TRANSITIONS = {
    "submitted": {"working", "canceled", "failed"},
    "working": {"input-required", "completed", "failed", "canceled"},
    "input-required": {"working", "canceled", "failed"},
    "completed": set(),
    "failed": set(),
    "canceled": set(),
}


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #
def test_states_are_exactly_the_six_a2a_states():
    assert ALL_STATES == {
        "submitted",
        "working",
        "input-required",
        "completed",
        "failed",
        "canceled",
    }
    assert TaskState.SUBMITTED == "submitted"
    assert TaskState.INPUT_REQUIRED == "input-required"


def test_transition_table_matches_the_spec():
    assert set(ALLOWED_TRANSITIONS) == set(EXPECTED_TRANSITIONS)
    for old, allowed in EXPECTED_TRANSITIONS.items():
        assert set(ALLOWED_TRANSITIONS[old]) == allowed, old


def test_active_and_terminal_states_partition_all_states():
    assert ACTIVE_STATES == {"submitted", "working", "input-required"}
    assert TERMINAL_STATES == {"completed", "failed", "canceled"}
    assert ACTIVE_STATES | TERMINAL_STATES == ALL_STATES
    assert ACTIVE_STATES & TERMINAL_STATES == set()


def test_terminal_states_have_no_outgoing_edges():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == set(), state
        for target in ALL_STATES:
            assert not models.can_transition(state, target), (state, target)


def test_no_state_transitions_to_itself():
    """``working -> working`` is not a transition; a repeat report is an event
    of its own kind (pulse), otherwise seq-based replay would double-apply."""

    for state in ALL_STATES:
        assert not models.can_transition(state, state), state


# --------------------------------------------------------------------------- #
# validate_transition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("old", "new"),
    sorted(
        (old, new)
        for old, targets in EXPECTED_TRANSITIONS.items()
        for new in targets
    ),
)
def test_every_legal_transition_is_accepted(old, new):
    assert validate_transition(old, new) == new
    assert models.can_transition(old, new) is True


@pytest.mark.parametrize(
    ("old", "new"),
    sorted(
        (old, new)
        for old, new in itertools.product(sorted(ALL_STATES), repeat=2)
        if new not in EXPECTED_TRANSITIONS[old]
    ),
)
def test_every_illegal_transition_raises_with_the_stable_message(old, new):
    with pytest.raises(ValueError) as excinfo:
        validate_transition(old, new)

    assert str(excinfo.value) == f"illegal transition {old}->{new}"
    assert models.can_transition(old, new) is False


def test_completed_to_working_is_rejected():
    """The one transition the story calls out by name."""

    with pytest.raises(ValueError, match=r"^illegal transition completed->working$"):
        validate_transition(TaskState.COMPLETED, TaskState.WORKING)

    assert models.can_transition("completed", "working") is False


@pytest.mark.parametrize("state", ["", "done", "WORKING", None, 3, True, ["working"]])
def test_unknown_states_are_rejected_without_type_errors(state):
    """Unhashable / non-string values must raise ``ValueError``, not
    ``TypeError`` from the set lookup."""

    with pytest.raises(ValueError, match="unknown task state"):
        models.validate_state(state)

    with pytest.raises(ValueError):
        validate_transition(state, "working")
    with pytest.raises(ValueError):
        validate_transition("working", state)

    assert models.can_transition(state, "working") is False
    assert models.can_transition("working", state) is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("submitted", False),
        ("working", False),
        ("input-required", False),
        ("completed", True),
        ("failed", True),
        ("canceled", True),
        ("nonsense", False),
    ],
)
def test_is_terminal(state, expected):
    assert models.is_terminal(state) is expected


# --------------------------------------------------------------------------- #
# Illegal transitions never reach the log
# --------------------------------------------------------------------------- #
def test_append_state_writes_a_legal_transition(isolated_workspace):
    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created", "title": "整理标书"})

    record = views.append_state(task_id, TaskState.WORKING, note="开工")

    assert record["seq"] == 2
    assert record["type"] == "state"
    assert record["value"] == "working"

    stored = store.read_events(task_id)[1]
    assert stored["value"] == "working"
    assert stored["note"] == "开工"
    assert views.materialize(task_id)["state"] == "working"


def test_illegal_transition_raises_and_writes_nothing(isolated_workspace, caplog):
    """``completed -> working``: ValueError, and the log is byte-identical."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created", "title": "整理标书"})
    views.append_state(task_id, TaskState.WORKING)
    views.append_state(task_id, TaskState.COMPLETED, summary="做完了")

    path = store.task_path(task_id)
    before_bytes = path.read_bytes()
    before_events = len(store.read_events(task_id))
    before_state = views.materialize(task_id)["state"]

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        with pytest.raises(ValueError) as excinfo:
            views.append_state(task_id, TaskState.WORKING, note="手滑")

    assert str(excinfo.value) == "illegal transition completed->working"
    assert path.read_bytes() == before_bytes, "a rejected transition must not append"
    assert len(store.read_events(task_id)) == before_events
    assert views.materialize(task_id)["state"] == before_state == "completed"
    # No lock file was left behind either.
    assert not (store._lock_dir() / f"{task_id}.lock").exists()


def test_append_state_rejects_a_transition_from_submitted_to_completed(
    isolated_workspace,
):
    """``submitted -> completed`` skips ``working``; the edge does not exist."""

    task_id = store.new_task_id()
    store.append_event(task_id, {"type": "created"})

    with pytest.raises(ValueError, match="illegal transition submitted->completed"):
        views.append_state(task_id, TaskState.COMPLETED)

    assert [event["type"] for event in store.read_events(task_id)] == ["created"]


def test_append_state_on_missing_task_starts_from_submitted(isolated_workspace):
    """No log yet → the materialised state is ``submitted``, so only its edges
    are legal. ``submitted -> working`` is one of them."""

    task_id = store.new_task_id()

    views.append_state(task_id, TaskState.WORKING)

    assert views.materialize(task_id)["state"] == "working"
