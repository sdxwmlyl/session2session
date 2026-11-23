"""A2A-style task states and the legal transition table (US-002).

A task starts at ``submitted`` and may only move along the edges declared in
:data:`ALLOWED_TRANSITIONS`. ``completed`` / ``failed`` / ``canceled`` are
terminal: they have no outgoing edge at all, which is what makes
``completed -> working`` (a finished task silently reopening) impossible
rather than merely "not recommended".

Everything here is plain data plus one validator: no filesystem access, no
host imports, so it stays importable in a bare interpreter.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ACTIVE_STATES",
    "ALLOWED_TRANSITIONS",
    "ALL_STATES",
    "TERMINAL_STATES",
    "TaskState",
    "can_transition",
    "is_terminal",
    "validate_state",
    "validate_transition",
]


class TaskState:
    """String constants for the six task states (``str``, not ``Enum``).

    Events are plain JSON, so the state has to be a JSON scalar; keeping the
    constants as ``str`` means an event read back from disk compares equal to
    the constant without any conversion.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


#: Every legal state value.
ALL_STATES: Final[frozenset[str]] = frozenset(
    {
        TaskState.SUBMITTED,
        TaskState.WORKING,
        TaskState.INPUT_REQUIRED,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }
)

#: States still being worked on (a task here shows up in ``/tasks``).
ACTIVE_STATES: Final[frozenset[str]] = frozenset(
    {
        TaskState.SUBMITTED,
        TaskState.WORKING,
        TaskState.INPUT_REQUIRED,
    }
)

#: States a task never leaves.
TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }
)

#: ``old state -> states it may move to``.
#:
#: Values are ``frozenset`` (an immutable ``set``) so the table cannot be
#: mutated at runtime by an importer; ``frozenset({"a"}) == {"a"}``, so plain
#: set comparisons in tests and callers still work.
#:
#: Terminal states map to the empty set on purpose: this is the single place
#: that forbids ``completed -> working``.
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    TaskState.SUBMITTED: frozenset(
        {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED}
    ),
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset(
        {TaskState.WORKING, TaskState.CANCELED, TaskState.FAILED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
}


def validate_state(state: object) -> str:
    """Return ``state`` if it is one of :data:`ALL_STATES`, else raise.

    A non-string (``None``, ``123``, a dict from a malformed event) is
    rejected here rather than compared: unhashable values would otherwise
    raise ``TypeError`` from the set lookup instead of a clear ``ValueError``.
    """

    if not isinstance(state, str) or state not in ALL_STATES:
        raise ValueError(f"unknown task state: {state!r}")
    return state


def can_transition(old: object, new: object) -> bool:
    """True when ``old -> new`` is a legal edge (never raises)."""

    if not isinstance(old, str) or not isinstance(new, str):
        return False
    if old not in ALL_STATES or new not in ALL_STATES:
        return False
    return new in ALLOWED_TRANSITIONS[old]


def validate_transition(old: object, new: object) -> str:
    """Validate and return ``new``; raise ``ValueError`` when illegal.

    The error message is stable (``"illegal transition completed->working"``)
    because the tool layer surfaces it to the model verbatim: the agent needs
    to see both endpoints to correct itself in one turn.

    Callers must validate *before* appending an event — the store itself is
    append-only and has no opinion about states.
    """

    validate_state(old)
    validate_state(new)
    if new not in ALLOWED_TRANSITIONS[old]:
        raise ValueError(f"illegal transition {old}->{new}")
    return new


def is_terminal(state: object) -> bool:
    """True when ``state`` is one of :data:`TERMINAL_STATES`."""

    return isinstance(state, str) and state in TERMINAL_STATES
