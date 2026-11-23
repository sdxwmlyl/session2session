"""Reliability layer (US-010): turn-end auto ``pulse`` + agent-stop guard.

Why this file exists
--------------------
A delegated task goes silent the moment the worker forgets to call
``task_pulse``: the initiator sees ``working`` forever, and the worker's turn
ends with nothing on the log. The story asks the host to close that gap from
the outside, with two runtime callbacks:

* a **runtime hook** for phase ``FINALLY`` (end of every agent turn) that
  appends one ``pulse`` to each of the session's ``working`` tasks when the
  turn produced no report/pulse of its own;
* an **agent stop handler** that refuses to end the turn (``BLOCK`` +
  ``continuation``) when a ``working`` task got neither, at most twice per
  task.

**Upstream status (verified 2026-11-22, QwenPaw 2.3.0).** Neither API exists:

* ``qwenpaw/plugins/api.py:49`` ``class PluginApi`` — the only ``register_*``
  methods are ``register_provider`` (:82), ``register_startup_hook`` (:128),
  ``register_shutdown_hook`` (:160), ``register_uninstall_hook`` (:192),
  ``register_workspace_created_hook`` (:235), ``register_http_router`` (:271),
  ``register_control_command`` (:302), ``register_prompt_section`` (:325),
  ``register_tool`` (:399), ``register_skill_provider`` (:514).
* ``hasattr(PluginApi, "register_runtime_hook") is False`` and
  ``hasattr(PluginApi, "register_agent_stop_handler") is False``.
* There is no hook-phase enum at all: grepping the package for ``FINALLY`` /
  ``HookPhase`` finds only comments and ``finally:`` blocks. The three hooks
  that do exist are process-level (startup/shutdown/uninstall/workspace
  created), not per-turn.

So this module is written *against the intended contract* and installed only
when a host exposes it (:func:`install` degrades to a warning otherwise). The
logic is proven by :mod:`tests.test_reliability` with a ``FakeApi``, so the
day upstream ships the two methods the wiring is one line in ``plugin.py``.

Contracts this module assumes (documented because they are not upstream's yet)
------------------------------------------------------------------------------
``register_runtime_hook(phase, callback)``
    ``phase`` is the string ``"FINALLY"``. ``callback`` is called once at the
    end of every agent turn with either a ``dict`` of context or keyword
    arguments. Recognised keys: ``session_id`` / ``session`` (defaults to
    :func:`tools.resolve_session`), ``turn_id`` / ``turn_key`` /
    ``request_id`` / ``message_id``, and ``assistant_text`` / ``text`` /
    ``last_assistant_text`` (the turn's final assistant message).

``register_agent_stop_handler(handler)``
    ``handler`` is called when the agent wants to end its turn, with the same
    context. It returns either ``None`` (allow) or a dict
    ``{"decision": "BLOCK", "blocked": True, "continuation": "...",
    "task_id": ..., "attempt": n}``. ``ALLOW`` is expressed as
    ``{"decision": "ALLOW", "blocked": False, ...}`` so the host never has to
    guess whether ``None`` meant "no opinion".

Turn boundary: event-sequence watermarks
----------------------------------------
"Did this turn report anything?" needs a turn boundary, and a clock is a poor
one: the event log stores seconds, the hook fires between two writes in the
same second, and two turns can be 40 ms apart. Instead :data:`_WATERMARK`
keeps, per task, the highest event ``seq`` seen when the previous turn ended.
Any ``report``/``pulse`` event **above** that watermark was written during the
current turn. ``seq`` is gap-free and monotonic per task
(:func:`store.append_event` assigns it inside the lock), so this is exact and
needs no wall clock.

Idempotency
-----------
Two independent guards, because a hook may be called twice for one turn
(exception path + normal path):

* ``(task_id, turn_key)`` in :data:`_PULSED` — this process already wrote the
  auto-pulse for that turn;
* the event's ``idempotency_key`` (``auto-pulse:<task>:<turn_key>``), which
  survives a process restart and is checked on disk by
  :func:`views.append_event_once`.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

try:  # loaded as a package by the host loader
    from . import store as _store
    from . import tools as _tools
    from . import views as _views
except ImportError:  # loaded as a top-level module (tests, plain sys.path)
    import store as _store  # type: ignore[no-redef]
    import tools as _tools  # type: ignore[no-redef]
    import views as _views  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

#: The hook phase the story asks for. Upstream has no phase enum yet (see the
#: module docstring), so the name is kept here instead of imported.
PHASE_FINALLY = "FINALLY"

#: Hook / handler names handed to the host (used for logs and unregistration).
FINALLY_HOOK_NAME = "session_task_finally_pulse"
STOP_HANDLER_NAME = "session_task_stop_guard"

#: ``task_report`` writes ``summary`` and ``task_pulse`` writes ``pulse``;
#: either means "this turn was alive". A bare ``state`` event is only a
#: transition — the move into ``working`` happens before the work starts — so
#: it counts only when it carries a ``note`` (the shape ``append_state`` uses
#: for a report that also moves the task).
ACTIVITY_TYPES = frozenset({"summary", "pulse"})

#: How many times one task may BLOCK the end of a turn; the third is let go,
#: so a worker that never reports still terminates instead of looping.
MAX_BLOCKS = 2

#: ``note`` length cap for an auto-pulse (story: first 80 chars of the turn).
NOTE_CHARS = 80

#: Context keys the host may use for each thing we need.
_SESSION_KEYS = ("session_id", "sessionId", "session")
_TURN_KEYS = ("turn_id", "turn_key", "request_id", "message_id")
_TEXT_KEYS = ("assistant_text", "last_assistant_text", "text", "summary")

#: ``(task_id, turn_key)`` already auto-pulsed in this process.
_PULSED: set[tuple[str, str]] = set()
#: ``task_id -> highest event seq seen at the end of the previous turn``.
_WATERMARK: dict[str, int] = {}
#: ``task_id -> how many times we have blocked the end of a turn``.
_BLOCKS: dict[str, int] = {}


def reset_state() -> None:
    """Forget every in-process mark. Tests and a plugin reload call this."""

    _PULSED.clear()
    _WATERMARK.clear()
    _BLOCKS.clear()


__all__ = [
    "ACTIVITY_TYPES",
    "FINALLY_HOOK_NAME",
    "MAX_BLOCKS",
    "NOTE_CHARS",
    "PHASE_FINALLY",
    "STOP_HANDLER_NAME",
    "auto_pulse",
    "install",
    "on_agent_stop",
    "on_turn_finally",
    "reset_state",
    "stop_decision",
]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def install(api: Any) -> dict:
    """Attach both callbacks to ``api`` where the host supports them.

    Reaches every method through ``getattr``: on QwenPaw 2.3.0 neither exists,
    and a plugin that raised here would lose its six delegation tools over an
    optional reliability feature.

    Args:
        api: the ``PluginApi`` instance handed to ``plugin.register``.

    Returns:
        dict: ``{"installed": [names], "missing": [names]}`` — ``missing``
        naming the exact method the host does not expose.
    """

    installed: list[str] = []
    missing: list[str] = []

    hook_register = getattr(api, "register_runtime_hook", None)
    if hook_register is None:
        missing.append("register_runtime_hook")
        logger.warning(
            "session-task: host has no register_runtime_hook; "
            "turn-end auto-pulse is unavailable (see README, 二期待做)"
        )
    else:
        try:
            try:
                hook_register(phase=PHASE_FINALLY, callback=on_turn_finally)
            except TypeError:
                # Signature drift: the two-argument positional form is the
                # documented second guess.
                hook_register(PHASE_FINALLY, on_turn_finally)
        except Exception:  # noqa: BLE001 - optional surface, never fatal
            logger.warning(
                "session-task: could not register the FINALLY runtime hook",
                exc_info=True,
            )
        else:
            installed.append("register_runtime_hook")
            logger.info(
                "session-task: FINALLY runtime hook registered (%s)", FINALLY_HOOK_NAME
            )

    stop_register = getattr(api, "register_agent_stop_handler", None)
    if stop_register is None:
        missing.append("register_agent_stop_handler")
        logger.warning(
            "session-task: host has no register_agent_stop_handler; "
            "the stop guard is unavailable (see README, 二期待做)"
        )
    else:
        try:
            try:
                stop_register(handler=on_agent_stop)
            except TypeError:
                stop_register(on_agent_stop)
        except Exception:  # noqa: BLE001 - optional surface, never fatal
            logger.warning(
                "session-task: could not register the agent stop handler",
                exc_info=True,
            )
        else:
            installed.append("register_agent_stop_handler")
            logger.info(
                "session-task: stop handler registered (%s)", STOP_HANDLER_NAME
            )

    return {"installed": installed, "missing": missing}


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
def _as_context(args: tuple, kwargs: dict) -> dict:
    """Merge the host's calling convention into one plain dict.

    Unknown positionals (a phase enum, an agent object) are ignored on
    purpose: guessing wrong is worse than falling back to
    :func:`tools.resolve_session`.
    """

    merged: dict[str, Any] = {}
    for value in args:
        if isinstance(value, dict):
            merged.update(value)
    merged.update(kwargs)
    return merged


def _first_str(context: dict, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _session_of(context: dict) -> str:
    """Session under test: the host's own id, else the tool context's."""

    explicit = _first_str(context, _SESSION_KEYS)
    if explicit is not None:
        return explicit
    return _tools.resolve_session()


def _turn_text(context: dict) -> str:
    """The turn's final assistant text, ``""`` when the host sends none."""

    explicit = _first_str(context, _TEXT_KEYS)
    return explicit or ""


def _turn_key(context: dict, text: str) -> str:
    """Stable id for the current turn.

    Preferred: whatever id the host passes. Otherwise the turn's own text is
    the only thing available, so it *is* the key — two turns with byte-identical
    final messages are treated as one. That only ever drops a redundant pulse
    (the store dedupes on the note as well), so it is the safe direction.
    """

    explicit = _first_str(context, _TURN_KEYS)
    if explicit is not None:
        return explicit
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"text:{digest}"


def _turn_note(text: str) -> str:
    """``text`` cut to the first :data:`NOTE_CHARS` characters."""

    return text.strip()[:NOTE_CHARS]


# --------------------------------------------------------------------------- #
# Log inspection
# --------------------------------------------------------------------------- #
def _max_seq(events: list[dict]) -> int:
    highest = 0
    for event in events:
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > highest:
            highest = seq
    return highest


def _working_rows(session: str) -> list[dict]:
    """The session's ``working`` tasks, newest first. Never raises."""

    try:
        return _views.iter_session_views(
            session, role="all", states=["working"], limit=None
        )
    except Exception:  # noqa: BLE001 - a hidden session must not break a turn
        logger.warning(
            "session-task: could not list working tasks for %r", session, exc_info=True
        )
        return []


def _is_activity(event: dict) -> bool:
    """True when ``event`` proves the turn reported or pulsed.

    ``summary`` / ``pulse`` are the two writers ``tools.py`` uses. A ``state``
    event only counts when it carries a non-empty ``note``: the initial
    ``submitted -> working`` transition is not a report, and treating it as
    one would silence the very first auto-pulse of every task.
    """

    event_type = event.get("type")
    if event_type in ACTIVITY_TYPES:
        return True
    if event_type == "state":
        note = event.get("note")
        return isinstance(note, str) and bool(note.strip())
    return False


def _has_activity(task_id: str, events: list[dict]) -> bool:
    """True when a report/pulse landed after the previous turn ended."""

    watermark = _WATERMARK.get(task_id, 0)
    for event in events:
        if not _is_activity(event):
            continue
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        if seq > watermark:
            return True
    return False


def _read_events(task_id: str) -> list[dict]:
    try:
        return _store.read_events(task_id)
    except Exception:  # noqa: BLE001 - unreadable log: treat as silent
        logger.warning(
            "session-task: could not read events for %s", task_id, exc_info=True
        )
        return []


# --------------------------------------------------------------------------- #
# FINALLY hook → auto pulse
# --------------------------------------------------------------------------- #
def on_turn_finally(*args: Any, **kwargs: Any) -> dict:
    """Runtime-hook entry point: append the missing heartbeat for this turn."""

    return auto_pulse(_as_context(args, kwargs))


def auto_pulse(context: Optional[dict] = None) -> dict:
    """Append one ``pulse`` per ``working`` task the turn left silent.

    Args:
        context: host context (see the module docstring); ``None`` means
            "resolve everything from the tool context".

    Returns:
        dict: ``{"session": str, "pulsed": [task_id], "skipped": [task_id]}``
        — ``skipped`` are the tasks that already reported/pulsed this turn or
        were pulsed earlier in the same turn.
    """

    context = dict(context or {})
    session = _session_of(context)
    text = _turn_text(context)
    key = _turn_key(context, text)
    note = _turn_note(text)

    pulsed: list[str] = []
    skipped: list[str] = []

    for row in _working_rows(session):
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue

        events = _read_events(task_id)

        if (task_id, key) in _PULSED:
            skipped.append(task_id)
            _WATERMARK[task_id] = max(_WATERMARK.get(task_id, 0), _max_seq(events))
            continue

        if _has_activity(task_id, events):
            skipped.append(task_id)
            _WATERMARK[task_id] = max(_WATERMARK.get(task_id, 0), _max_seq(events))
            continue

        try:
            _views.append_event_once(
                task_id,
                {
                    "type": "pulse",
                    "value": note,
                    "idempotency_key": f"auto-pulse:{task_id}:{key}",
                    "auto": True,
                },
            )
        except Exception:  # noqa: BLE001 - one bad task, not a dead hook
            logger.warning(
                "session-task: could not auto-pulse %s", task_id, exc_info=True
            )
            skipped.append(task_id)
            continue

        _PULSED.add((task_id, key))
        pulsed.append(task_id)
        logger.info("session-task: auto-pulse for %s (%d chars)", task_id, len(note))
        _WATERMARK[task_id] = _max_seq(_read_events(task_id))

    if pulsed:
        logger.info(
            "session-task: turn finished with %d auto-pulse(s) in %s",
            len(pulsed),
            session,
        )
    return {"session": session, "pulsed": pulsed, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Agent stop handler → BLOCK
# --------------------------------------------------------------------------- #
def on_agent_stop(*args: Any, **kwargs: Any) -> dict:
    """Agent-stop entry point: block the turn once while work is unreported."""

    return stop_decision(_as_context(args, kwargs))


def stop_decision(context: Optional[dict] = None) -> dict:
    """Decide whether the agent may end its turn.

    A ``working`` task the turn neither reported nor pulsed earns a ``BLOCK``
    with a ``continuation`` telling the worker which tool to call. Per task
    that happens at most :data:`MAX_BLOCKS` times; the next attempt is allowed
    so an agent that cannot report (tool error, no permission) still finishes
    instead of looping until the task timeout.

    Args:
        context: host context (see the module docstring).

    Returns:
        dict: ``{"decision": "BLOCK"|"ALLOW", "blocked": bool,
        "continuation": str|None, "task_id": str|None, "attempt": int}``.
    """

    context = dict(context or {})
    session = _session_of(context)
    text = _turn_text(context)
    key = _turn_key(context, text)

    for row in _working_rows(session):
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue

        events = _read_events(task_id)
        if _has_activity(task_id, events) or (task_id, key) in _PULSED:
            _WATERMARK[task_id] = max(_WATERMARK.get(task_id, 0), _max_seq(events))
            continue

        attempt = _BLOCKS.get(task_id, 0) + 1
        if attempt > MAX_BLOCKS:
            logger.warning(
                "session-task: letting %s go after %d blocks without a report",
                task_id,
                MAX_BLOCKS,
            )
            _BLOCKS[task_id] = attempt
            _WATERMARK[task_id] = max(_WATERMARK.get(task_id, 0), _max_seq(events))
            continue

        _BLOCKS[task_id] = attempt
        continuation = (
            f"任务 {task_id} 还在进行，请 task_report 或 task_pulse 后再结束"
        )
        logger.info(
            "session-task: blocking turn end for %s (attempt %d/%d)",
            task_id,
            attempt,
            MAX_BLOCKS,
        )
        return {
            "decision": "BLOCK",
            "blocked": True,
            "continuation": continuation,
            "task_id": task_id,
            "attempt": attempt,
        }

    return {
        "decision": "ALLOW",
        "blocked": False,
        "continuation": None,
        "task_id": None,
        "attempt": 0,
    }
