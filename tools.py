"""The six delegation tools exposed to the agent (US-003).

The layer is deliberately thin: every tool is a validated wrapper around
``store`` / ``models`` / ``views``. There is no state of its own here, and no
host import at module level: ``qwenpaw`` / ``agentscope`` symbols are imported
inside the functions that need them, so this file stays importable in a bare
interpreter (which is how the tests run).

Contract shared by all six tools
--------------------------------
* **Arguments are validated before anything is written.** A blank id, an
  unknown state, an illegal transition or a missing task all come back as
  ``{"ok": False, "error": "..."}`` — the model gets a sentence it can act on,
  never a traceback. Nothing raises out of a tool.
* **``from_session`` is not a parameter.** The origin session is injected by
  the host: :func:`_resolve_session` reads the ``current_session_id``
  context variable that ``qwenpaw.app.runner`` sets for every request
  (``set_current_session_id`` in ``qwenpaw/app/agent_context.py``, called from
  ``app/runner/runner.py`` and ``agents/react_agent.py``). If that is not
  available (plain script, background thread) it falls back to the
  ``QWENPAW_SESSION_ID`` environment variable, then to ``unknown:unknown``.
* **Every write carries an idempotency key** (``<task_id>:<kind>:<digest>``),
  so a retried tool call cannot produce a second ``completed`` event or a
  second copy of a summary.
* **Terminal tasks are frozen, but a retry is not a mistake.** ``completed``
  / ``failed`` / ``canceled`` have no outgoing edge in
  :data:`models.ALLOWED_TRANSITIONS`, so a finished task cannot be silently
  reopened. A *replay* of the write that ended the task — the same terminal
  report, or the same cancel reason — answers ``{"ok": true, ...}`` without
  touching the log, because "did my earlier call land?" must be answerable.
  A genuinely different change is refused.
* **``working -> working`` is legal for the tools, not for the state
  machine.** ``task_report(state="working")`` on a task already ``working``
  refreshes ``summary`` / ``artifacts`` and appends **no** state event: an
  agent must be able to say "still going, here is the new detail" in the
  middle of a long task.
* **Only participants may read or advance a task.** ``task_get`` requires
  the caller to be the task's ``from_session`` or ``to_session``;
  ``task_report`` / ``task_pulse`` / ``task_cancel`` require the same, per the
  PRD ("发起方与接收方都有权推进"). Any other session gets
  ``{"ok": false, "error": "无权访问该任务"}`` — knowing a ``task_id`` is not
  authority over it. The one exception is a test harness: a call attributed
  to ``unknown:unknown`` is allowed when ``QWENPAW_TASK_ALLOW_SYSTEM=1``
  (off by default).
* **Every string that reaches the log has a length budget.** A 10k title used
  to inflate one event to 60 KB and one ``task_list`` row to 11.7 KB; the caps
  live in the ``MAX_*`` constants below and are enforced *before* the first
  write.
* **Return shape.** On success ``task_add`` returns the new ``task_id``
  string, ``task_list`` returns a list of summaries, ``task_get`` returns the
  materialised view, and the three writers return
  ``{"ok": True, "task_id": ..., "state": ...}``.
* **Annotations are PEP 563 strings** (``from __future__ import
  annotations``) and the host's JSON-schema builder evaluates them inside
  ``agentscope._utils._common``, whose namespace holds only the builtins it
  imports. So every annotation must resolve from builtins alone: write
  ``list[str] | None``, never ``Optional[list[str]]`` — the latter raises
  ``PydanticUserError: ... you should define Optional`` at registration time
  (found with the host interpreter, not in the bare test run).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

try:  # imported as part of the plugin package (host loader)
    from . import delivery, models, prompt, store, views
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    import delivery  # type: ignore[no-redef]
    import models  # type: ignore[no-redef]
    import prompt  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]
    import views  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

#: Environment variable the host may set when the context variable is absent.
SESSION_ENV = "QWENPAW_SESSION_ID"

#: Attribution used when neither the host nor the environment names a session.
FALLBACK_SESSION = "unknown:unknown"

#: States ``task_report`` may move a task to. ``submitted`` is where a task
#: starts (never a report target) and ``canceled`` belongs to ``task_cancel``;
#: both are refused here so the two tools keep one meaning each.
REPORTABLE_STATES: tuple[str, ...] = (
    models.TaskState.WORKING,
    models.TaskState.INPUT_REQUIRED,
    models.TaskState.COMPLETED,
    models.TaskState.FAILED,
)

#: Opt-in switch that lets a call attributed to :data:`FALLBACK_SESSION`
#: (``unknown:unknown`` — a bare script, a cron job, a test harness) reach any
#: task. **Off by default**: without it the fallback session is treated like
#: any other stranger. ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case
#: insensitive) turn it on.
ALLOW_SYSTEM_ENV = "QWENPAW_TASK_ALLOW_SYSTEM"

#: Values of :data:`ALLOW_SYSTEM_ENV` that mean "on".
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Length budgets, in characters. They exist because the event log is read
#: back into the prompt and into ``task_list``: one unbounded field turns a
#: single delegation into a 60 KB event and an 11 KB list row.
MAX_TITLE = 200
MAX_GOAL = 4000
MAX_SUMMARY = 8000
MAX_NOTE = 500
MAX_REASON = 1000
#: ``artifacts`` is a list, so it carries two budgets: how many entries and
#: how long each one may be.
MAX_ARTIFACT_ITEMS = 50
MAX_ARTIFACT_CHARS = 500
#: ``task_list`` is a prompt section, not a document: a long title is cut to
#: this many characters (ellipsis included) in the summary row.
LIST_TITLE_CHARS = 80

#: ``agentscope`` types are optional: the host provides them, a bare
#: interpreter does not. ``None`` means "return the plain payload", which is
#: what the tests assert against.
try:  # pragma: no cover - depends on host installation
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse
except Exception:  # ImportError, but tolerate any host-side failure
    TextBlock = None  # type: ignore[assignment]
    ToolResponse = None  # type: ignore[assignment]

__all__ = [
    "ALLOW_SYSTEM_ENV",
    "FALLBACK_SESSION",
    "LIST_TITLE_CHARS",
    "MAX_ARTIFACT_CHARS",
    "MAX_ARTIFACT_ITEMS",
    "MAX_GOAL",
    "MAX_NOTE",
    "MAX_REASON",
    "MAX_SUMMARY",
    "MAX_TITLE",
    "NOTIFY_STATES",
    "REPORTABLE_STATES",
    "SESSION_ENV",
    "ToolError",
    "resolve_session",
    "task_add",
    "task_cancel",
    "task_get",
    "task_list",
    "task_pulse",
    "task_report",
]


class ToolError(Exception):
    """A rejection that belongs in ``{"ok": false, "error": ...}``.

    Used for caller mistakes (blank id, unknown state, frozen task) so the
    message reaches the model verbatim instead of being masked by a generic
    "internal error".
    """


# --------------------------------------------------------------------------- #
# Session, digests, responses
# --------------------------------------------------------------------------- #
def resolve_session(env: str = SESSION_ENV, fallback: str = FALLBACK_SESSION) -> str:
    """Return the session this tool call originates from.

    **Host injection point.** QwenPaw binds the calling session to a
    ``ContextVar`` before running the agent (``set_current_session_id``), and
    :func:`qwenpaw.app.agent_context.get_current_session_id` reads it from any
    tool running inside that request. That value always wins.

    Order: host context variable → ``env`` → ``fallback``. The last resort is
    a deliberate literal (never an empty string) so a task created outside the
    host is still traceable to *something* instead of an anonymous log line.
    """

    try:
        from qwenpaw.app.agent_context import get_current_session_id
    except Exception:  # host package absent, or a broken install
        get_current_session_id = None  # type: ignore[assignment]

    if get_current_session_id is not None:
        try:
            injected = get_current_session_id()
        except Exception:  # pragma: no cover - context var misuse
            logger.debug("get_current_session_id() raised; falling back", exc_info=True)
            injected = None
        if isinstance(injected, str) and injected.strip():
            return injected.strip()

    from_env = os.environ.get(env, "")
    if isinstance(from_env, str) and from_env.strip():
        return from_env.strip()

    return fallback


#: Backwards-friendly alias used in the public API description.
_resolve_session = resolve_session


def _digest(*parts: Any) -> str:
    """Short stable hash of the semantic inputs of one write.

    Two retries of the same tool call produce the same digest and therefore
    the same idempotency key; a genuinely different report does not.
    """

    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _error(exc: BaseException) -> dict:
    """Turn any exception into the tool-level error envelope."""

    if isinstance(exc, ToolError):
        message = str(exc)
    elif isinstance(exc, ValueError):  # models / views / store validation
        message = str(exc)
    else:
        message = f"{type(exc).__name__}: {exc}"
        logger.warning("session-task tool failed: %s", message, exc_info=True)
    return {"ok": False, "error": message}


def _respond(payload: Any) -> Any:
    """Wrap ``payload`` in a ``ToolResponse`` when the host provides one.

    A bare interpreter (tests) gets the payload itself, which keeps the
    assertions readable and means the tools can be exercised without the
    QwenPaw package installed.
    """

    if ToolResponse is None or TextBlock is None:
        return payload
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return ToolResponse(content=[TextBlock(type="text", text=text)])


def _require_text(value: Any, name: str, max_chars: int | None = None) -> str:
    """Return the stripped text in ``value``, or raise :class:`ToolError`.

    ``max_chars`` is checked on the value that is about to be stored, not on
    the caller's padded original, so a string that only fits after trimming is
    accepted and one that does not is refused with a message naming the field
    and the budget.
    """

    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} 必须是非空字符串")
    text = value.strip()
    if max_chars is not None and len(text) > max_chars:
        raise ToolError(f"{name} 超过 {max_chars} 字符上限")
    return text


def _optional_text(value: Any, name: str, max_chars: int) -> str:
    """Return ``value`` as stripped text, allowing ``""`` but nothing else.

    The old behaviour coerced anything to ``str`` (``note=42`` became
    ``"42"``, ``note=None`` became ``""``) and answered ``ok``, so a
    mis-typed argument vanished into a log line nobody could explain. Only a
    string is accepted now; a missing argument keeps its ``""`` default.
    """

    if not isinstance(value, str):
        raise ToolError(f"{name} 必须是字符串")
    text = value.strip()
    if len(text) > max_chars:
        raise ToolError(f"{name} 超过 {max_chars} 字符上限")
    return text


def _normalise_artifacts(value: Any) -> list[str]:
    """Validate ``artifacts`` into a list of non-empty strings.

    ``None`` means "no artifacts" (the documented default). A single string is
    one artifact. Anything else must be a list/tuple whose every item is a
    ``str``: silently stringifying an element (``{'a': 1}`` -> ``"{'a': 1}"``,
    ``7`` -> ``"7"``, ``None`` -> ``"None"``) wrote paths that never existed.
    """

    if value is None:
        return []

    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise ToolError("artifacts 必须是字符串列表")
    if len(items) > MAX_ARTIFACT_ITEMS:
        raise ToolError(f"artifacts 超过 {MAX_ARTIFACT_ITEMS} 项上限")

    result: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ToolError("artifacts 必须是字符串列表")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_ARTIFACT_CHARS:
            raise ToolError(f"artifacts 单项超过 {MAX_ARTIFACT_CHARS} 字符上限")
        result.append(text)
    return result


def _truncate(value: Any, max_chars: int = LIST_TITLE_CHARS) -> Any:
    """Cut ``value`` to ``max_chars`` characters, ellipsis included."""

    if not isinstance(value, str) or len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def _summary_row(entry: dict) -> dict:
    """One ``task_list`` row, with its title held to :data:`LIST_TITLE_CHARS`."""

    if not isinstance(entry, dict):
        return entry
    row = dict(entry)
    row["title"] = _truncate(row.get("title"))
    return row


def _system_allowed() -> bool:
    """True when the ``unknown:unknown`` bypass was switched on explicitly."""

    raw = os.environ.get(ALLOW_SYSTEM_ENV, "")
    return raw.strip().lower() in _TRUTHY


def _is_privileged(session: Any) -> bool:
    """True for the opt-in internal/test caller described in the module docs."""

    return session == FALLBACK_SESSION and _system_allowed()


def _require_participant(view: dict, session: str) -> None:
    """Allow only the task's ``from_session`` or ``to_session`` to read it.

    Before this check any session that learned a ``task_id`` could read a
    stranger's task — and, through the writers, cancel it.
    """

    if _is_privileged(session):
        return
    if session not in (view.get("from_session"), view.get("to_session")):
        raise ToolError("无权访问该任务")


def _require_writer(view: dict, session: str) -> None:
    """Same membership rule, for the tools that advance the task.

    Kept as its own name (rather than reusing the participant check inline)
    because the PRD states the rule per role — "发起方与接收方都有权推进" — and
    a future rule for one of them (e.g. only ``to`` may report) belongs here.
    """

    if _is_privileged(session):
        return
    if session not in (view.get("from_session"), view.get("to_session")):
        raise ToolError("无权访问该任务")


def _load_view(task_id: Any) -> dict:
    """Materialise an existing task or raise :class:`ToolError`.

    ``views.materialize`` answers for unknown ids with an empty ``submitted``
    view, which would let a writer invent a task out of a typo. Existence is
    checked here, once, for all three writers and ``task_get``.
    """

    task_id = _require_text(task_id, "task_id")
    if not store.read_events(task_id):
        raise ToolError(f"任务不存在：{task_id}")
    return views.materialize(task_id)


def _reject_terminal(view: dict, action: str) -> None:
    """Refuse to touch a task that already ended."""

    state = view.get("state")
    if models.is_terminal(state):
        raise ToolError(f"任务 {view.get('task_id')} 已处于终态 {state}，不能再{action}")


def _already_applied(task_id: str, key: str) -> bool:
    """True when an event carrying ``key`` is already on disk.

    This is the read side of the idempotency contract: a retried tool call
    must be able to answer "did my earlier write land?" without re-validating
    it. ``views.append_state`` applies the same rule internally; the tools
    check here as well because they write *before* the state event, and a
    retry must not fail on ``working -> working`` just because the summary
    write needs the same decision.
    """

    return any(
        event.get("idempotency_key") == key for event in store.read_events(task_id)
    )


def _report_matches(view: dict, summary: str, artifacts: list[str]) -> bool:
    """True when ``view`` already holds exactly this summary and artifact list.

    Used only for the "same terminal state, but the state event's key is not on
    disk" case (a task finished by something other than this exact call). The
    normal retry path is :func:`_already_applied`, which compares digests and
    therefore also distinguishes two reports that merely look alike.
    """

    stored = [item for item in (view.get("artifacts") or []) if isinstance(item, str)]
    return view.get("summary") == summary and stored == artifacts


def _write_summary(task_id: str, summary: str, artifacts: list[str]) -> None:
    """Append the summary (and, when given, the artifact list) events.

    Both calls are idempotent on content, so replaying the same report writes
    nothing. The artifact event is skipped when the list is empty: an absent
    list means "no change", not "erase what is there".
    """

    if artifacts:
        views.append_event_once(
            task_id,
            {
                "type": "artifacts",
                "value": artifacts,
                "idempotency_key": f"{task_id}:artifacts:{_digest(artifacts)}",
            },
        )
    views.append_event_once(
        task_id,
        {
            "type": "summary",
            "value": summary,
            "idempotency_key": f"{task_id}:summary:{_digest(summary)}",
        },
    )


def _cancel_note(task_id: str) -> Any:
    """The ``note`` of the last ``canceled`` state event, or ``None``.

    ``task_cancel`` compares it with the new reason to tell a retry (same
    reason, answer ``ok``) from a second, different cancellation (refuse).
    """

    for event in reversed(store.read_events(task_id)):
        if (
            event.get("type") == "state"
            and event.get("value") == models.TaskState.CANCELED
        ):
            note = event.get("note")
            return note if isinstance(note, str) else ""
    return None


# --------------------------------------------------------------------------- #
# Delivery (US-009)
# --------------------------------------------------------------------------- #
#: States worth pushing to the initiator's channel. ``working`` is not: a
#: worker that announces "started" would page the user once per task for
#: nothing, and ``task_pulse`` already covers long silences.
NOTIFY_STATES: tuple[str, ...] = (
    models.TaskState.INPUT_REQUIRED,
    models.TaskState.COMPLETED,
    models.TaskState.FAILED,
    models.TaskState.CANCELED,
)


async def _notify(session_id: str, text: str, what: str) -> dict:
    """Push ``text`` to ``session_id``; never raises, never changes state.

    Delivery is a side effect of a task transition, not part of it: the event
    log is already on disk when this runs, and a channel being down must not
    turn a recorded task into an error response. Failures are logged as
    warnings and reported in the returned dict.
    """

    try:
        result = await delivery.deliver_async(session_id, text)
    except Exception:  # noqa: BLE001 - a transport crash is not a tool error
        logger.warning("session-task: %s delivery crashed", what, exc_info=True)
        return {"ok": False, "error": "crashed"}

    if not result.get("ok"):
        logger.warning(
            "session-task: %s to %s not delivered: %s",
            what,
            session_id,
            result.get("error"),
        )
    return result


async def _notify_initiator(task_id: str, state: str) -> None:
    """Send the one-line summary of ``state`` to the initiator's session."""

    if state not in NOTIFY_STATES:
        return
    try:
        view = views.materialize(task_id)
        text = prompt.initiator_summary(view)
    except Exception:  # noqa: BLE001 - a broken summary must not fail the report
        logger.warning(
            "session-task: could not build the initiator summary",
            exc_info=True,
        )
        return

    await _notify(str(view.get("from_session") or ""), text, f"{state} summary")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
async def task_add(
    to_session: str,
    title: str,
    goal: str,
    artifacts: list[str] | None = None,
) -> str:
    """把一项任务委派给另一个会话，并登记成一条可追踪的任务。

    触发词：委派、派给、交给、转给、让另一个会话做、新建任务、登记任务。
    只在“这件事应该由别的会话去做”时调用；发起方会话由宿主自动记录，
    不要（也无法）通过参数传入。

    Args:
        to_session (str):
            接收任务的会话 ID，例如 "sub-1a2b3c4d"；不能为空。
        title (str):
            任务标题，一句话，会显示在任务列表里；最长 200 字符。
        goal (str):
            任务目标与验收标准，接收方据此开工；最长 4000 字符。
        artifacts (list[str] | None):
            可选：任务需要读取或产出的文件路径列表。缺省表示没有产物；
            最多 50 项，每项最长 500 字符。

    Returns:
        str: 新任务的 task_id（形如 "t-20261122-2115-1a2b3c4d"）。
        失败时返回 {"ok": false, "error": "..."}。
    """

    try:
        to_session = _require_text(to_session, "to_session")
        title = _require_text(title, "title", MAX_TITLE)
        goal = _require_text(goal, "goal", MAX_GOAL)
        artifact_list = _normalise_artifacts(artifacts)
        from_session = resolve_session()

        task_id = store.new_task_id()
        views.append_event_once(
            task_id,
            {
                "type": "created",
                "title": title,
                "goal": goal,
                "from_session": from_session,
                "to_session": to_session,
                "idempotency_key": (
                    f"{task_id}:created:"
                    f"{_digest(title, goal, from_session, to_session)}"
                ),
            },
        )
        if artifact_list:
            views.append_event_once(
                task_id,
                {
                    "type": "artifacts",
                    "value": artifact_list,
                    "idempotency_key": f"{task_id}:artifacts:{_digest(artifact_list)}",
                },
            )
        logger.info(
            "task %s created: %s -> %s (%s)",
            task_id,
            from_session,
            to_session,
            title,
        )
        # The worker learns about the task from this card. A card that cannot
        # be built is a courtesy lost, not a task lost — the id is returned
        # either way and ``task_get`` always works.
        try:
            card = prompt.worker_task_card(views.materialize(task_id))
        except Exception:  # noqa: BLE001 - the task already exists
            logger.warning(
                "session-task: could not build the task card", exc_info=True
            )
        else:
            await _notify(to_session, card, "task card")
        return _respond(task_id)
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return _respond(_error(exc))


async def task_list(role: str = "from", limit: int = 20) -> list:
    """列出当前会话的任务摘要，用于汇报进度或先看有没有重复委派。

    触发词：任务列表、我派出去的任务、有哪些任务、看看进度、清单。
    只返回摘要（task_id / 标题 / 状态 / 发起方 / 接收方 / 更新时间），
    要看完整内容请用 task_get。

    Args:
        role (str):
            "from" = 我发起的（默认），"to" = 派给我的，"all" = 两者都算。
        limit (int):
            最多返回多少条，默认 20；0 表示只要空列表，负数被拒绝，
            超过 50 会自动按 50 处理。

    Returns:
        list: 摘要列表，按更新时间倒序（title 超过 80 字符会截断并加 …）；
        失败时返回 {"ok": false, "error": "..."}。
    """

    try:
        session_id = resolve_session()
        if not isinstance(role, str) or not role.strip():
            raise ToolError("role 必须是 from / to / all 之一")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ToolError("limit 必须是整数")
        if limit < 0:
            # ``list[-1]`` used to reach ``views`` and silently return [].
            raise ToolError("limit 必须是非负整数")
        return _respond(
            [
                _summary_row(entry)
                for entry in views.index_for_session(
                    session_id,
                    role=role.strip(),
                    limit=min(limit, views.SUMMARY_LIMIT),
                )
            ]
        )
    except Exception as exc:  # noqa: BLE001
        return _respond(_error(exc))


async def task_get(task_id: str) -> dict:
    """查看一个任务的完整视图：状态、目标、摘要、产物与全部历史事件。

    触发词：看看任务、任务详情、查任务、这个任务到哪了、历史。
    拿不准 task_id 时先用 task_list。只有任务的发起方与接收方能查看。

    Args:
        task_id (str):
            任务 ID，由 task_add 返回，例如 "t-20261122-2115-1a2b3c4d"。

    Returns:
        dict: materialize 出来的完整视图（task_id / state / title / goal /
        from_session / to_session / summary / artifacts / history /
        updated_at）；任务不存在或调用方不是参与方时返回
        {"ok": false, "error": "..."}。
    """

    try:
        session = resolve_session()
        view = _load_view(task_id)
        _require_participant(view, session)
        return _respond(view)
    except Exception as exc:  # noqa: BLE001
        return _respond(_error(exc))


async def task_report(
    task_id: str,
    state: str,
    summary: str,
    artifacts: list[str] | None = None,
) -> dict:
    """汇报任务进度或结果，把状态推进到 working / input-required /
    completed / failed，并写入摘要与产物。

    触发词：汇报、报告进度、更新状态、我做完了、完成了、卡住了、失败了、
    需要补充信息。做完任务必须调用一次，否则发起方只能看到 submitted。
    只有任务的发起方与接收方可以汇报。任务还在 working 时再报 working
    只更新摘要与产物、不重复记状态；终态任务只接受内容完全相同的重试。

    Args:
        task_id (str):
            要更新的任务 ID。
        state (str):
            目标状态，只能是 "working" / "input-required" / "completed" /
            "failed"；"submitted" 与 "canceled" 不接受（取消请用 task_cancel）。
        summary (str):
            这一轮的结果摘要，一句话说清做了什么、结论是什么；不能为空，
            最长 8000 字符。
        artifacts (list[str] | None):
            可选：当前产物路径的**完整列表**（会整体替换旧列表，不是追加）；
            最多 50 项，每项最长 500 字符。

    Returns:
        dict: {"ok": true, "task_id": ..., "state": ...}；
        状态非法、迁移非法、不是参与方或任务已终态且内容不同时返回
        {"ok": false, "error": "..."}。
    """

    try:
        task_id = _require_text(task_id, "task_id")
        if not isinstance(state, str) or state not in REPORTABLE_STATES:
            raise ToolError(
                "state 只能是 " + " / ".join(REPORTABLE_STATES) + f"，收到 {state!r}"
            )
        summary = _require_text(summary, "summary", MAX_SUMMARY)
        artifact_list = _normalise_artifacts(artifacts)

        session = resolve_session()
        view = _load_view(task_id)
        _require_writer(view, session)

        current = view["state"]
        state_key = f"{task_id}:{state}:{_digest(state, summary, artifact_list)}"

        if models.is_terminal(current):
            # First: is this a replay of the very call that ended the task?
            # Answering ``ok`` here is what makes a lost response retryable
            # without reopening anything.
            if _already_applied(task_id, state_key):
                return _respond({"ok": True, "task_id": task_id, "state": current})
            if state == current:
                if _report_matches(view, summary, artifact_list):
                    return _respond(
                        {"ok": True, "task_id": task_id, "state": current}
                    )
                raise ToolError("任务已完成，不能再修改")
            _reject_terminal(view, "汇报")

        # A retry of a non-terminal write that already landed.
        if _already_applied(task_id, state_key):
            return _respond({"ok": True, "task_id": task_id, "state": current})

        if state == models.TaskState.WORKING and current == models.TaskState.WORKING:
            # Still working, new detail. No state event: ``working -> working``
            # is not an edge, and re-stamping it would only pad the history.
            _write_summary(task_id, summary, artifact_list)
            logger.info("task %s summary refreshed (%s)", task_id, summary[:60])
            return _respond({"ok": True, "task_id": task_id, "state": current})

        # Validate *before* the first write: a rejected report must not leave
        # a summary/artifacts event behind for a transition that never
        # happened. ``views.append_state`` re-validates under its lock.
        models.validate_transition(current, state)

        _write_summary(task_id, summary, artifact_list)
        views.append_state(
            task_id,
            state,
            note=summary,
            idempotency_key=state_key,
        )
        logger.info("task %s -> %s (%s)", task_id, state, summary[:60])
        await _notify_initiator(task_id, state)
        return _respond({"ok": True, "task_id": task_id, "state": state})
    except Exception as exc:  # noqa: BLE001
        return _respond(_error(exc))


async def task_pulse(task_id: str, note: str = "") -> dict:
    """给正在做的任务打一个心跳，表示“还在推进”，不改变任务状态。

    触发词：心跳、还在做、进度更新、打个点、别让它看起来卡住。
    长任务每隔一段时间调用一次；同一 note 重复调用只会记一条。
    只有任务的发起方与接收方能打心跳，终态任务一律拒绝。

    Args:
        task_id (str):
            要打点的任务 ID。
        note (str):
            可选：这一拍在做什么，一句话；必须是字符串，最长 500 字符。

    Returns:
        dict: {"ok": true, "task_id": ..., "state": ...}；
        任务已终态、不存在或调用方不是参与方时返回
        {"ok": false, "error": "..."}。
    """

    try:
        task_id = _require_text(task_id, "task_id")
        note_text = _optional_text(note, "note", MAX_NOTE)
        session = resolve_session()
        view = _load_view(task_id)
        _require_writer(view, session)
        _reject_terminal(view, "打心跳")

        views.append_event_once(
            task_id,
            {
                "type": "pulse",
                "value": note_text,
                "idempotency_key": f"{task_id}:pulse:{_digest(note_text)}",
            },
        )
        return _respond({"ok": True, "task_id": task_id, "state": view["state"]})
    except Exception as exc:  # noqa: BLE001
        return _respond(_error(exc))


async def task_cancel(task_id: str, reason: str = "") -> dict:
    """取消一个任务，把它推进到 canceled 并记下原因。

    触发词：取消、撤销、不做了、终止任务、取消委派。
    已经结束的任务（completed / failed / canceled）不能再取消；
    只有任务的发起方与接收方能取消。同一原因的重复取消返回 ok，
    换一个原因的取消被拒绝。

    Args:
        task_id (str):
            要取消的任务 ID。
        reason (str):
            可选：取消原因，会写进事件日志；必须是字符串，最长 1000 字符。

    Returns:
        dict: {"ok": true, "task_id": ..., "state": "canceled"}；
        任务已终态（且原因不同）、不存在或调用方不是参与方时返回
        {"ok": false, "error": "..."}。
    """

    try:
        task_id = _require_text(task_id, "task_id")
        reason_text = _optional_text(reason, "reason", MAX_REASON)
        session = resolve_session()
        view = _load_view(task_id)
        _require_writer(view, session)

        if models.is_terminal(view["state"]):
            # Retry of the cancel that landed: same reason, same answer.
            if (
                view["state"] == models.TaskState.CANCELED
                and _cancel_note(task_id) == reason_text
            ):
                return _respond(
                    {"ok": True, "task_id": task_id, "state": models.TaskState.CANCELED}
                )
            _reject_terminal(view, "取消")

        views.append_state(
            task_id,
            models.TaskState.CANCELED,
            note=reason_text,
            idempotency_key=(
                f"{task_id}:canceled:{_digest(models.TaskState.CANCELED, reason_text)}"
            ),
        )
        logger.info("task %s canceled (%s)", task_id, reason_text or "no reason")
        await _notify_initiator(task_id, models.TaskState.CANCELED)
        return _respond(
            {"ok": True, "task_id": task_id, "state": models.TaskState.CANCELED}
        )
    except Exception as exc:  # noqa: BLE001
        return _respond(_error(exc))
