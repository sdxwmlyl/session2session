"""Materialised views over the append-only task log (US-002).

The event log is the only source of truth; :func:`materialize` replays it
into the small dict the rest of the plugin (tools, HTTP, prompt digest)
reads. Nothing here caches or writes state of its own.

Event vocabulary::

    {"type": "created", "title": ..., "goal": ...,
     "from_session": ..., "to_session": ...}   # task metadata (once)
    {"type": "state", "value": "working"}     # a transition (see models)
    {"type": "summary", "value": "..."}       # replaces the summary text
    {"type": "artifacts", "value": [...]}     # replaces the artifact list
    {"type": "pulse"|"nudge"|...}             # history only, no view effect

Two deliberate leniencies, both so one bad event cannot make a task
unreadable:

* a ``state`` event whose value is unknown or whose transition is illegal is
  logged and **skipped** — the view keeps the last legal state instead of
  raising;
* an event whose ``type`` is itself a state name (``{"type": "completed"}``)
  is treated as that transition, because that is the shape later stories
  write (``accepted`` / ``completed`` events, see US-009). Unknown types are
  history-only.

``history`` keeps *every* readable event in file order, including the ones
whose effect was skipped: it is the log, not a filtered trace, and a reader
debugging a stuck task needs to see the illegal attempt.

Writes go through :func:`append_state`, which holds a per-task
``threading.RLock`` across ``materialize -> validate -> append``. That closes
the race *inside one process*; the same window in another process is still
open, and the docstring says so rather than implying otherwise.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, Optional

try:  # imported as part of the plugin package
    from . import store
    from .models import ALL_STATES, TaskState, validate_transition
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    import store  # type: ignore[no-redef]
    from models import ALL_STATES, TaskState, validate_transition  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

#: Hard cap on :func:`index_for_session` results (a session with 500 tasks
#: must not build a 500-entry prompt section).
SUMMARY_LIMIT = 50

#: Accepted ``role`` filter values.
ROLES = ("from", "to", "all")

#: Keys every :func:`materialize` result carries, in a stable order.
VIEW_KEYS = (
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
)

#: Serialises "scan for the key, then append" inside one process. Cross
#: process idempotency is best-effort: the store lock makes each append
#: atomic but cannot make read-then-write atomic across processes.
_IDEMPOTENCY_GUARD = threading.Lock()

#: Guards :data:`_TASK_LOCKS` (the map), never the work done under a task
#: lock. Kept separate so creating a lock for one task cannot block another.
_TASK_LOCKS_GUARD = threading.Lock()

#: One re-entrant lock per task id, so :func:`append_state` can hold the
#: "read the view, validate, append" window closed inside this process.
#: Typed ``Any`` because ``threading.RLock`` is a factory function, not a
#: class, so annotating the values with it would not type-check.
#: Entries are never removed: a lock is a few dozen bytes, and deleting one
#: that another thread is waiting on would reopen the very race it prevents.
_TASK_LOCKS: dict[str, Any] = {}

__all__ = [
    "ROLES",
    "SUMMARY_LIMIT",
    "VIEW_KEYS",
    "append_event_once",
    "append_state",
    "index_for_session",
    "iter_session_views",
    "materialize",
]

#: Keys of one :func:`index_for_session` row. A row is a *summary*: the whole
#: view minus ``history`` / ``artifacts`` / ``goal`` / ``summary``.
SUMMARY_KEYS = ("task_id", "title", "state", "from", "to", "updated_at")

#: Extra key :func:`iter_session_views` rows carry on top of
#: :data:`SUMMARY_KEYS`: the ``at`` of the newest ``pulse`` event, or ``None``.
#: Computed from the history the scan already materialised, so a caller that
#: shows "last heartbeat" does not read every log a second time.
PULSE_KEY = "last_pulse_at"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _event_value(event: dict) -> Any:
    """The payload of ``event``: ``value``, else a field named after its type.

    ``{"type": "summary", "value": "..."}`` and the terser
    ``{"type": "summary", "summary": "..."}`` both work, so neither writer
    shape loses data silently.
    """

    if "value" in event:
        return event["value"]
    event_type = event.get("type")
    if isinstance(event_type, str):
        return event.get(event_type)
    return None


def _state_of_event(event: dict) -> Optional[str]:
    """The state an event implies, or ``None`` when it is not a transition."""

    event_type = event.get("type")
    if event_type == "state":
        return event.get("value")
    if isinstance(event_type, str) and event_type in ALL_STATES:
        return event_type
    return None


def _normalise_artifacts(value: Any) -> list[Any]:
    """Artifacts are always a list, whatever a writer sent."""

    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _warn_state_event(task_id: str, event: dict, reason: str) -> None:
    """Log one unusable state event without letting it affect the view."""

    logger.warning(
        "skipping state event %s for %s: %s", event.get("seq"), task_id, reason
    )


def _task_lock(task_id: str) -> Any:
    """Return this process's re-entrant ``threading.RLock`` for ``task_id``.

    Re-entrant because :func:`append_state` may be reached from a helper that
    already holds it; a plain ``Lock`` would then deadlock on itself.
    """

    with _TASK_LOCKS_GUARD:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[task_id] = lock
        return lock


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def append_event_once(task_id: str, event: dict) -> dict:
    """Append ``event`` unless an event with the same ``idempotency_key`` exists.

    A retried tool call (timeout, agent loop, HTTP replay) carries the same
    key and must not produce a second ``completed`` event. Without a key this
    is exactly :func:`store.append_event`.

    Returns the record that is on disk: the existing one when the key was
    already seen, otherwise the freshly written one.
    """

    if not isinstance(event, dict):
        raise TypeError("event must be a dict")

    key = event.get("idempotency_key")
    if key is None:
        return store.append_event(task_id, event)

    with _IDEMPOTENCY_GUARD:
        for existing in store.read_events(task_id):
            if existing.get("idempotency_key") == key:
                logger.debug(
                    "ignoring duplicate event %r for %s (seq %s)",
                    key,
                    task_id,
                    existing.get("seq"),
                )
                return existing
        return store.append_event(task_id, event)


def append_state(task_id: str, new_state: str, **fields: Any) -> dict:
    """Move a task to ``new_state``; raise without writing when illegal.

    This is the only supported way to move a task: the check runs against the
    state materialised from the log, so a stale caller cannot reopen a
    finished task. ``fields`` are merged into the event (``note``,
    ``summary``, ``artifacts``, ``idempotency_key`` ...); ``type`` and
    ``value`` are not overridable.

    Order matters, and it is: **idempotency key first, then validation**.
    A retried call that carries a key already on disk returns that event
    untouched — no validation, no write — because "did my earlier call land?"
    must be answerable even when the answer is "yes, and it finished the
    task": re-validating would raise ``completed->completed`` at a caller who
    did nothing wrong.

    The whole ``materialize -> validate -> append`` window runs under the
    per-task lock from :func:`_task_lock`, so two threads of this process
    cannot both validate against the same ``working`` view and both "succeed"
    while only one of them survives replay. A loser waits, re-materialises,
    sees the winner's state, and gets a ``ValueError`` instead of a silent
    no-op.

    Scope of that guarantee: **one process only**. The lock is a
    ``threading.RLock`` in this interpreter; another process (a second worker,
    a CLI invocation) still has its own ``materialize -> append`` window, and
    the store's lock file only makes each *append* atomic, not the check
    around it. Two processes racing the same task can therefore still produce
    one rejected-on-replay event. Closing that needs a compare-and-append (or
    a versioned head) in the store, which US-002 deliberately does not
    attempt; meanwhile the operating assumption stands that a task has one
    active writer (its ``to_session``).
    """

    key = fields.get("idempotency_key")

    with _task_lock(task_id):
        if key is not None:
            for existing in store.read_events(task_id):
                if existing.get("idempotency_key") == key:
                    logger.debug(
                        "append_state %r for %s already applied at seq %s",
                        key,
                        task_id,
                        existing.get("seq"),
                    )
                    return existing

        view = materialize(task_id)
        validate_transition(view["state"], new_state)

        event: dict[str, Any] = dict(fields)
        event["type"] = "state"
        event["value"] = new_state
        return append_event_once(task_id, event)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def materialize(task_id: str) -> dict:
    """Replay the event log for ``task_id`` into the current view.

    Always returns exactly :data:`VIEW_KEYS`. An unknown ``task_id`` (no log
    file) yields a ``submitted`` view with empty history rather than an
    exception: callers that must 404 check the file themselves.
    """

    view: dict[str, Any] = {
        "task_id": task_id,
        "state": TaskState.SUBMITTED,
        "title": None,
        "goal": None,
        "from_session": None,
        "to_session": None,
        "summary": None,
        "artifacts": [],
        "history": [],
        "updated_at": None,
    }

    for event in store.read_events(task_id):
        event_type = event.get("type")
        view["history"].append(
            {
                "type": event_type,
                "value": _event_value(event),
                "at": event.get("at"),
                "seq": event.get("seq"),
            }
        )

        at = event.get("at")
        if isinstance(at, str) and at:
            view["updated_at"] = at

        if event_type == "created":
            for field in ("title", "goal", "from_session", "to_session"):
                if field in event:
                    view[field] = event[field]
            continue

        if event_type == "summary":
            view["summary"] = _event_value(event)
            continue

        if event_type == "artifacts":
            view["artifacts"] = _normalise_artifacts(_event_value(event))
            continue

        if event_type == "state" and "value" not in event:
            _warn_state_event(task_id, event, "no value")
            continue

        new_state = _state_of_event(event)
        if new_state is None:
            continue

        try:
            view["state"] = validate_transition(view["state"], new_state)
        except ValueError as exc:
            # One bad event must not blank the whole view: keep the last legal
            # state and leave the attempt visible in history.
            _warn_state_event(task_id, event, str(exc))

    return view


def _validate_session_query(session_id: str, role: str) -> None:
    """Reject a bad session id / role before touching the filesystem."""

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r} (expected one of {ROLES})")


def _last_pulse_at(view: dict) -> Optional[str]:
    """``at`` of the newest ``pulse`` event in an already materialised view.

    Read from ``view["history"]`` instead of the log so a listing pays for one
    materialisation per task, not one per task plus one per shown row.
    """

    last: Optional[str] = None
    for entry in view.get("history") or ():
        if not isinstance(entry, dict) or entry.get("type") != "pulse":
            continue
        at = entry.get("at")
        if isinstance(at, str) and at:
            last = at
    return last


def _session_aliases(session_id: str) -> frozenset[str]:
    """Every spelling of ``session_id`` we treat as the same session.

    A channel-qualified id (``console:sub-1a2b3c4d``) and the bare id the
    tools write into the event log (``sub-1a2b3c4d``) name the same session:
    the host context gives the tools the raw id, while HTTP/session listings
    arrive channel-qualified. Matching only the exact string made a correct
    task invisible to ``GET /api/session-task/tasks?session_id=console:<id>``.
    """

    aliases = {session_id}
    _, separator, tail = session_id.partition(":")
    if separator and tail:
        aliases.add(tail)
    return frozenset(aliases)


def _involves(view: dict, session_id: str, role: str) -> bool:
    """True when ``view`` matches ``role`` for ``session_id``."""

    wanted = _session_aliases(session_id)
    if role == "from":
        return bool(wanted & _session_aliases(view["from_session"] or ""))
    if role == "to":
        return bool(wanted & _session_aliases(view["to_session"] or ""))
    return bool(
        wanted & _session_aliases(view["from_session"] or "")
    ) or bool(wanted & _session_aliases(view["to_session"] or ""))


def _scan_session(session_id: str, role: str) -> list[dict]:
    """Every task this session is involved in, as a summary row, uncapped.

    One ``materialize`` per task on disk: the event log is the only source of
    truth and nothing writes ``index/`` yet, so there is no materialised view
    to read first. The session/role test runs *here*, on the view and before
    any sort or cap — that is what lets a caller filter by state without a
    50-row window hiding an older task (US-005 defect: an old active task
    behind 50 finished ones disappeared from the digest and from ``/tasks``).
    """

    try:
        paths = sorted(store.events_dir().glob("*.jsonl"))
    except OSError:  # pragma: no cover - unreadable directory
        logger.warning("could not list session task events", exc_info=True)
        return []

    rows: list[dict] = []
    for path in paths:
        try:
            view = materialize(path.stem)
        except Exception:  # noqa: BLE001 - one bad file must not hide the rest
            logger.warning("could not materialize task %s", path.stem, exc_info=True)
            continue

        if not _involves(view, session_id, role):
            continue

        rows.append(
            {
                "task_id": view["task_id"],
                "title": view["title"],
                "state": view["state"],
                "from": view["from_session"],
                "to": view["to_session"],
                "updated_at": view["updated_at"],
                PULSE_KEY: _last_pulse_at(view),
            }
        )

    return rows


def _sort_rows(rows: list[dict]) -> list[dict]:
    """Newest first, ties broken by ``task_id`` descending.

    ``at`` has second resolution, so several tasks updated in the same second
    would otherwise come back in whatever order the directory listing
    happened to produce.
    """

    return sorted(
        rows,
        key=lambda row: (
            row["updated_at"] is not None,
            row["updated_at"] or "",
            row["task_id"],
        ),
        reverse=True,
    )


def iter_session_views(
    session_id: str,
    role: str = "all",
    *,
    states: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """One row per task ``session_id`` is involved in: filter, sort, cap.

    A row is a :data:`SUMMARY_KEYS` summary plus :data:`PULSE_KEY` — a view
    *onto* the task, not the whole ``materialize`` payload. Carrying ``goal``
    / ``summary`` / ``artifacts`` / ``history`` for every task in a session
    would make a prompt build pay for fields it never prints; the one derived
    field that is worth it (the last pulse) is computed here from the
    materialisation the scan already has, so no log is read twice.

    ``role``: ``"from"`` = tasks I delegated, ``"to"`` = tasks handed to me,
    ``"all"`` = either.

    ``states``: optional collection of states to keep
    (``models.ACTIVE_STATES``, one state, or ``None`` for every state). An
    unknown value raises ``ValueError``: a typo must not silently answer
    "nothing is active".

    ``limit``: optional cap.

    The order of operations is the point of this function: **session/role →
    state → sort → cap**. :func:`index_for_session` caps first and filters
    afterwards, which is fine for a plain "newest N" list but blind to an old
    active task sitting behind 50 finished ones. Anything that filters — the
    prompt digest, ``/tasks``, the HTTP list — must come through here.

    Returns:
        list[dict]: rows carrying :data:`SUMMARY_KEYS` plus :data:`PULSE_KEY`,
        newest first.
    """

    _validate_session_query(session_id, role)

    state_filter: Optional[frozenset[str]] = None
    if states is not None:
        try:
            state_filter = frozenset(states)
        except TypeError as exc:  # not iterable / unhashable member
            raise ValueError(f"states must be an iterable of states: {exc}") from exc
        unknown = state_filter - ALL_STATES
        if unknown:
            raise ValueError(f"unknown task state: {sorted(unknown)!r}")

    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an int or None")
        if limit < 0:
            raise ValueError("limit must be non-negative")

    rows = _scan_session(session_id, role)
    if state_filter is not None:
        rows = [row for row in rows if row["state"] in state_filter]
    rows = _sort_rows(rows)
    if limit is not None:
        rows = rows[:limit]
    return rows


def index_for_session(
    session_id: str, role: str = "all", limit: int = SUMMARY_LIMIT
) -> list[dict]:
    """Summaries of the tasks this session sent or received.

    ``role``: ``"from"`` = tasks I delegated, ``"to"`` = tasks handed to me,
    ``"all"`` = either. Ordered by ``updated_at`` descending (newest first),
    capped at :data:`SUMMARY_LIMIT` entries.

    This is the "newest N tasks" view: the cap applies to the session's tasks
    as a whole, so a caller that then filters by state is looking at a window,
    not at the session (see :func:`iter_session_views` for that). The rows are
    exactly :data:`SUMMARY_KEYS`; the scan is shared with
    :func:`iter_session_views`, so both answer from one materialisation per
    task.
    """

    _validate_session_query(session_id, role)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an int")
    limit = min(limit, SUMMARY_LIMIT)
    if limit <= 0:
        return []

    return [
        {key: row[key] for key in SUMMARY_KEYS}
        for row in iter_session_views(session_id, role=role, limit=limit)
    ]
