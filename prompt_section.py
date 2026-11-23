"""Live-session prompt section and ``/tasks`` control commands (US-005).

Three surfaces, one source of truth (the event log, replayed by
``views.materialize``):

* :func:`digest` / :func:`provider` — the dynamic prompt section. A session
  that has work in flight gets a compact block of its active tasks, so the
  model does not have to call ``task_list`` on every turn to remember them.
  Empty string when there is nothing active, which is how the host's
  ``PromptBuilder`` drops the section entirely (``_render`` skips falsy text).
* :func:`handle_tasks` / :func:`handle_task` / :func:`handle_nudge` — the
  command payloads, ``async (ctx, args) -> str``.
* :class:`TasksCommandHandler` and friends — thin adapters to the host's
  control-command contract (see "Command contract" below).

Command contract (checked against the installed host, not guessed)
-------------------------------------------------------------------
``qwenpaw/plugins/api.py`` has **no** ``register_command`` / ``register_slash``.
The only command channel a plugin gets is
``PluginApi.register_control_command(handler, priority_level=10)``
(``plugins/api.py:302``); the host keeps those in
``qwenpaw/app/runner/control_commands/__init__.py`` and dispatches them
through ``handle_control_command(query, context)``. A handler must therefore
look like ``qwenpaw/app/runner/control_commands/base.py``::

    class BaseControlCommandHandler(ABC):
        command_name: str
        description: str
        async def handle(self, context: ControlContext) -> str: ...

``command_name`` has **no single correct shape** in this host: two call sites
read the same attribute and want opposite spellings.

* ``qwenpaw/app/_app.py:406`` (and ``app/routers/plugins.py:183``) build the
  *channel* registry key as ``f"/{handler.command_name}"`` — so the value has
  to be slash-free (``"tasks"``). Declaring ``"/tasks"`` there produced the
  key ``"//tasks"``, ``channels/base.py:1172`` stopped recognising ``/tasks``
  and the command fell through to TaskTracker (US-005 defect).
* ``qwenpaw/app/runner/control_commands/__init__.py:62`` keys the *dispatch*
  registry with ``handler.command_name.lower()`` and looks handlers up with
  the slashed token from the query (``"/tasks"``) — so the value has to keep
  the slash, or ``runner.query_handler`` never routes to the handler at all.

:class:`CommandName` resolves that by being slash-free when it is formatted
(the bridge) and slashed in ``lower()`` (the dispatch key). That is one
object, one registration, one advertised command. A plain ``"tasks"`` would
silently kill dispatch; a plain ``"/tasks"`` is the ``//tasks`` defect.

The dispatcher sends the handler's returned **string** straight back to the
channel (``return await handler.handle(context)``). ``context.args`` is a
``dict`` built by ``parse_args`` — positional words land in
``args["_raw_args"]`` because the default parser only understands
``key=value`` pairs. The pure handlers below accept ``args`` as a string, a
dict or ``None`` and normalise it, so both the host path and a direct test
call work.

``agentscope.message.Msg`` exists in the host but is not part of the
control-command contract; returning one would hand the channel a ``Msg``
where it expects text. Every handler here returns a plain ``str``, and the
docstrings say so instead of silently diverging from the host.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

try:  # imported as part of the plugin package (host loader)
    from . import models, store, tools, views
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    import models  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]
    import tools  # type: ignore[no-redef]
    import views  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

__all__ = [
    "COMMAND_HANDLERS",
    "CommandName",
    "MAX_DIGEST_CHARS",
    "MAX_DIGEST_LINES",
    "MAX_DIGEST_TITLE_CHARS",
    "NudgeCommandHandler",
    "SECTION_ANCHOR",
    "SECTION_FALLBACK_ANCHOR",
    "SECTION_NAME",
    "TaskCommandHandler",
    "TasksCommandHandler",
    "digest",
    "handle_nudge",
    "handle_task",
    "handle_tasks",
    "provider",
    "relative_time",
]

#: Name the section registers under (unique per host registry).
SECTION_NAME = "session-task-protocol"

#: Anchor the story asks for. The installed host only accepts the anchors in
#: ``qwenpaw/agents/prompt_builder.py:30`` — ``("workspace", "multimodal",
#: "env_context")`` — and raises ``ValueError`` for anything else, so
#: :func:`plugin.SessionTaskPlugin._register_prompt_section` retries with
#: :data:`SECTION_FALLBACK_ANCHOR` and logs the downgrade.
SECTION_ANCHOR = "workspace/AGENTS.md"
SECTION_FALLBACK_ANCHOR = "workspace"

#: The section is paid for on every turn of the session: five lines, 600
#: characters, both asserted in ``tests/test_prompt_and_commands.py``.
MAX_DIGEST_LINES = 5
MAX_DIGEST_CHARS = 600
MAX_DIGEST_TITLE_CHARS = 40

#: Title length caps tried in order when the assembled block is over budget.
_TITLE_SHRINK_STEPS = (MAX_DIGEST_TITLE_CHARS, 24, 16, 8, 4, 0)

#: How many finished tasks ``/tasks`` lists.
DONE_LIMIT = 5

#: How many rows each ``/tasks`` group may show.
GROUP_LIMIT = 10

#: How many history events ``/task`` names in one line.
HISTORY_TAIL = 3


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #
def _one_line(value: Any) -> str:
    """``value`` as one stripped line (``""`` for anything unusable)."""

    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = text.replace("\t", " ")
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, marking the cut with ``…``.

    The ellipsis counts, so ``len(_clip(text, n)) <= n`` for every ``n``.
    """

    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _as_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO8601 timestamp from the event log, or return ``None``.

    Records are written with ``datetime.now().astimezone().isoformat()``, but
    a hand-edited or legacy line may be naive or plain garbage; both are
    tolerated here so one odd event cannot blank the section.
    """

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def relative_time(value: Any, now: Optional[datetime] = None) -> str:
    """Short human age of an ISO8601 timestamp, in Chinese.

    ``刚刚`` / ``N 分钟前`` / ``N 小时前`` / ``N 天前`` / ``MM-DD`` for older
    items, and ``未知`` when the value cannot be parsed. A timestamp in the
    future (clock skew between two machines) reads as ``刚刚`` rather than as
    a negative age.
    """

    parsed = _as_datetime(value)
    if parsed is None:
        return "未知"

    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    delta = reference - parsed
    if delta < timedelta(0):
        delta = timedelta(0)

    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 7 * 86400:
        return f"{days(delta)} 天前"
    return parsed.strftime("%m-%d")


def days(delta: timedelta) -> int:
    """Whole days in ``delta`` (helper kept separate for readability)."""

    return max(int(delta.total_seconds()) // 86400, 1)


# --------------------------------------------------------------------------- #
# Prompt section
# --------------------------------------------------------------------------- #
def _session_of(ctx: Any) -> str:
    """Resolve the session behind a context object, dict, or ``None``.

    Order: explicit ``session_id`` on the context → the host's injected
    context variable (``tools.resolve_session``). The prompt builder runs
    inside the agent's request, so the context variable is normally set; when
    it is not (a bare test process), the env/fallback chain applies.
    """

    candidate: Any = None
    if isinstance(ctx, dict):
        candidate = ctx.get("session_id")
    elif ctx is not None:
        candidate = getattr(ctx, "session_id", None)

    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return tools.resolve_session()


def _args_of(ctx: Any) -> Any:
    """The raw arguments attached to a context object or dict."""

    if isinstance(ctx, dict):
        return ctx.get("args", "")
    return getattr(ctx, "args", "")


def _args_text(args: Any) -> str:
    """Normalise command arguments to one stripped line.

    Accepts the three shapes that reach a handler in practice:

    * a plain string (the tests, and any caller that parses the command
      itself);
    * ``ControlContext.args`` — a dict from ``parse_args`` whose positional
      words live in ``"_raw_args"`` (the default parser only splits
      ``key=value``);
    * ``None`` / anything unusable → ``""``.
    """

    if isinstance(args, str):
        return _one_line(args)
    if isinstance(args, dict):
        raw = args.get("_raw_args")
        if isinstance(raw, str) and raw.strip():
            return _one_line(raw)
        parts = [
            f"{key}={value}"
            for key, value in args.items()
            if not str(key).startswith("_")
        ]
        return _one_line(" ".join(parts))
    return ""


def _split_id(args: Any) -> tuple[str, str]:
    """Split ``"<id> [note...]"`` into the task id and the remaining note."""

    text = _args_text(args)
    if not text:
        return "", ""
    head, _, tail = text.partition(" ")
    return head.strip(), tail.strip()


def _command_args(ctx: Any, args: Any) -> Any:
    """Prefer the explicit ``args``; fall back to the ones on ``ctx``.

    The host hands a handler ``context.args`` and nothing else, while a test
    may call ``handle_task(ctx, "t-...")``. Both shapes are accepted here
    instead of making the adapter and the pure handler disagree.
    """

    if _args_text(args):
        return args
    return _args_of(ctx)


def _active_entries(session_id: str) -> list[dict]:
    """Active tasks this session is involved in, newest first, **uncapped**.

    ``views.iter_session_views`` filters by session/role/state *before* it
    sorts and caps, so a busy day of finished tasks cannot push an older
    active one out of a window (US-005 defect). ``prompt_section`` used to
    ask ``index_for_session`` for the newest 50 tasks and drop the finished
    ones afterwards — 50 freshly completed tasks made the digest empty and
    ``/tasks`` report "0".
    """

    if not isinstance(session_id, str) or not session_id.strip():
        return []
    try:
        return views.iter_session_views(
            session_id.strip(), role="all", states=models.ACTIVE_STATES
        )
    except ValueError:
        return []


def _same_session(left: Any, right: Any) -> bool:
    """True when ``left`` and ``right`` name the same session.

    Thin wrapper over :func:`views._session_aliases`, so the commands and the
    list/digest paths share **one** rule: a channel-qualified id
    (``console:sub-1a2b3c4d``) and the bare id the tools write into the event
    log (``sub-1a2b3c4d``) are the same session. Comparing the raw strings
    used to drop a session's active tasks from ``/tasks`` and to refuse the
    legitimate initiator in ``/task`` and ``/nudge`` whenever the context
    arrived qualified.
    """

    a = _one_line(left)
    b = _one_line(right)
    if not a or not b:
        return False
    if a == b:
        return True
    return bool(views._session_aliases(a) & views._session_aliases(b))


def _pulse_text(entry: dict, now: Optional[datetime]) -> str:
    """``3 分钟前`` / ``未打点`` for one scanned row."""

    pulse_at = entry.get(views.PULSE_KEY)
    if isinstance(pulse_at, str) and pulse_at:
        return relative_time(pulse_at, now)
    return "未打点"


def _digest_line(entry: dict, title_limit: int, now: Optional[datetime]) -> str:
    """``id | 标题 | 状态 | 最近心跳`` for one entry.

    A literal ``|`` inside the title is normalised to ``｜``: the model reads
    the fields by splitting on the delimiter, so a title that smuggles one in
    would shift every field after it.
    """

    task_id = _one_line(entry.get("task_id")) or "?"
    title = (
        _clip(_one_line(entry.get("title")) or "未命名任务", title_limit).replace(
            "|", "｜"
        )
        or "—"
    )
    state = (_one_line(entry.get("state")) or "?").replace("|", "｜")
    pulse = _pulse_text(entry, now)
    return f"{task_id} | {title} | {state} | {pulse}"


def digest(
    session_id: str,
    now: Optional[datetime] = None,
    max_lines: int = MAX_DIGEST_LINES,
) -> str:
    """The prompt block for ``session_id``, or ``""`` when nothing is active.

    One line per task (at most :data:`MAX_DIGEST_LINES`)::

        【会话任务】2 个进行中（每行：id | 标题 | 状态 | 最近心跳）
        t-20261122-2115-1a2b3c4d | 整理回款明细 | working | 3 分钟前
        详情 task_get(id)；/tasks 看全部。

    The whole block never exceeds :data:`MAX_DIGEST_CHARS`: titles are
    shrunk first, then trailing lines are dropped, then the text is clipped.
    A section that pushes the system prompt over budget is worse than a
    section that names four tasks instead of five.
    """

    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 1:
        raise ValueError("max_lines must be a positive int")

    entries = _active_entries(session_id)
    if not entries:
        return ""

    shown = entries[: min(max_lines, MAX_DIGEST_LINES)]
    header = (
        f"【会话任务】{len(entries)} 个进行中" f"（每行：id｜标题｜状态｜最近心跳）"
    )
    footer = "详情 task_get(id)；/tasks 看全部。"

    lines = [_digest_line(entry, MAX_DIGEST_TITLE_CHARS, now) for entry in shown]
    text = "\n".join([header, *lines, footer])

    for title_limit in _TITLE_SHRINK_STEPS:
        if len(text) <= MAX_DIGEST_CHARS:
            break
        lines = [_digest_line(entry, title_limit, now) for entry in shown]
        text = "\n".join([header, *lines, footer])

    while len(text) > MAX_DIGEST_CHARS and lines:
        lines.pop()
        text = "\n".join([header, *lines, footer])

    return _clip(text, MAX_DIGEST_CHARS)


# --- 任务协议（随 digest 一起注入；2026-11-23 新增）-----------------------
SESSION_TASK_RULES = (
    "【任务协议·必读】"
    "① 收到任务卡（submitted）后，第一件事 task_report(state=working)，"
    "让发起方看到已接单；"
    "② 本轮结束前，若你名下有 working/submitted 的任务，必须 task_report 终态："
    "completed（摘要 + 产物路径）或 failed / input-required（写清卡在哪）；"
    "③ 做完了但状态仍是 submitted，视为本次执行失败；"
    "④ 你只对自己名下任务负责，不要替别的会话报。"
)


def provider(agent: Any = None) -> str:
    """``register_prompt_section`` provider: ``agent -> section text``.

    The host calls this as ``provider(agent)`` for every prompt build
    (``qwenpaw/agents/prompt_builder.py:_render``) and drops falsy results, so
    returning ``""`` for "no active task" is the whole inject/omit rule.

    Errors are swallowed *here* as well as in the host: a broken event log
    must not fail a prompt build. The host logs the exception either way; this
    keeps the same guarantee when the function is called directly.
    """

    try:
        text = digest(_session_of(agent))
        if not text:
            return ""
        return text + "\n" + SESSION_TASK_RULES
    except Exception:  # noqa: BLE001 - never break a prompt build
        logger.warning("session-task: prompt digest failed", exc_info=True)
        return ""


# --------------------------------------------------------------------------- #
# /tasks
# --------------------------------------------------------------------------- #
def _task_line(entry: dict, now: Optional[datetime]) -> str:
    """One ``/tasks`` row: ``• id 标题 [状态] 3 分钟前``."""

    task_id = _one_line(entry.get("task_id")) or "?"
    title = _clip(_one_line(entry.get("title")) or "未命名任务", MAX_DIGEST_TITLE_CHARS)
    state = _one_line(entry.get("state")) or "?"
    return (
        f"• {task_id} {title} [{state}] {relative_time(entry.get('updated_at'), now)}"
    )


def _group(title: str, entries: list[dict], now: Optional[datetime]) -> list[str]:
    """A group heading followed by its rows (or a ``（无）`` placeholder)."""

    lines = [f"{title}（{len(entries)}）"]
    if not entries:
        lines.append("  （无）")
    else:
        lines.extend("  " + _task_line(entry, now) for entry in entries)
    return lines


async def handle_tasks(ctx: Any, args: Any = "") -> str:
    """``/tasks`` — three groups: to me / by me / finished.

    Args:
        ctx: context with ``session_id`` (a ``ControlContext``, a dict, or
            ``None`` to use the host's injected session).
        args: unused; accepted so every command handler has the same shape.

    Returns:
        str: the listing, or a one-line hint when this session has no task at
        all. The host dispatcher publishes this string verbatim.

    One scan of the session, three groups. The role/state split runs on the
    *whole* session and only then is each group truncated: slicing the newest
    50 tasks first (the US-005 behaviour) reported "待我处理（0）" for a
    session whose one active task was older than 50 finished ones.
    """

    session_id = _session_of(ctx)
    now = datetime.now().astimezone()

    try:
        rows = views.iter_session_views(session_id, role="all")
    except ValueError as exc:  # pragma: no cover - session id already checked
        return f"任务列表读取失败：{exc}"

    incoming = [
        row
        for row in rows
        if _same_session(row["to"], session_id)
        and row["state"] in models.ACTIVE_STATES
    ][:GROUP_LIMIT]
    outgoing = [
        row
        for row in rows
        if _same_session(row["from"], session_id)
        and not models.is_terminal(row["state"])
    ][:GROUP_LIMIT]
    finished = [row for row in rows if models.is_terminal(row["state"])][:DONE_LIMIT]

    if not (incoming or outgoing or finished):
        return "当前会话没有任务。用 task_add 把活派给别的会话，或等别人派给你。"

    lines = [f"会话任务（{session_id}）"]
    lines += _group("待我处理", incoming, now)
    lines += _group("我发起的", outgoing, now)
    lines += _group("已完成", finished, now)
    lines.append("用 /task <id> 看详情，/nudge <id> [备注] 催办。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /task <id>
# --------------------------------------------------------------------------- #
def _artifact_lines(view: dict) -> list[str]:
    """Up to three artifact paths, with a ``+N`` tail when there are more."""

    artifacts = view.get("artifacts")
    if not isinstance(artifacts, (list, tuple)) or not artifacts:
        return []
    lines = [f"  - {_clip(_one_line(item) or '?', 120)}" for item in artifacts[:3]]
    if len(artifacts) > 3:
        lines.append(f"  … 另有 {len(artifacts) - 3} 个产物")
    return lines


async def handle_task(ctx: Any, args: Any = "") -> str:
    """``/task <id>`` — the materialised view of one task, as text.

    Only the task's two participants may read it, which mirrors
    ``tools.task_get``: a command must not widen what a session can see.

    Returns:
        str: a multi-line summary, ``"用法：/task <id>"`` when the id is
        missing, ``"任务不存在：<id>"`` for an unknown id, or the refusal text
        for a non-participant.
    """

    task_id, _ = _split_id(_command_args(ctx, args))
    if not task_id:
        return "用法：/task <id>（id 可以先 /tasks 查）"

    session_id = _session_of(ctx)
    view = _load_view(task_id)
    if view is None:
        return f"任务不存在：{task_id}"
    if not tools._is_privileged(session_id):
        if not (
            _same_session(session_id, view.get("from_session"))
            or _same_session(session_id, view.get("to_session"))
        ):
            return f"无权访问该任务（{task_id} 与你无关）"

    now = datetime.now().astimezone()
    history = view.get("history") or []
    tail = "、".join(
        _one_line(item.get("type")) or "?"
        for item in history[-HISTORY_TAIL:]
        if isinstance(item, dict)
    )

    lines = [
        f"任务 {view.get('task_id')}",
        f"状态：{_one_line(view.get('state')) or '?'}｜"
        f"标题：{_one_line(view.get('title')) or '未命名任务'}",
        f"发起：{_one_line(view.get('from_session')) or '?'} → "
        f"接收：{_one_line(view.get('to_session')) or '?'}",
        f"更新：{relative_time(view.get('updated_at'), now)}",
    ]
    goal = _clip(_one_line(view.get("goal")), 200)
    if goal:
        lines.append(f"目标：{goal}")
    summary = _clip(_one_line(view.get("summary")), 200)
    if summary:
        lines.append(f"摘要：{summary}")
    lines += _artifact_lines(view)
    if tail:
        lines.append(f"历史：{len(history)} 条（最近：{tail}）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /nudge <id> [note]
# --------------------------------------------------------------------------- #
async def handle_nudge(ctx: Any, args: Any = "") -> str:
    """``/nudge <id> [note]`` — the *initiator* pings a quiet worker.

    Writes a ``nudge`` event that carries the note and **never touches the
    state**: a reminder is not evidence of progress, so promoting
    ``submitted`` to ``working`` here would let a human's impatience fake a
    worker's activity. The worker sees the event in ``task_get`` history.

    Permission is the point of the command: only ``from_session`` may nudge.
    The recipient nudging itself is noise, and anyone else is a stranger.

    Returns:
        str: a confirmation, ``"用法：/nudge <id> [备注]"`` when the id is
        missing, ``"任务不存在：<id>"``, or the refusal text for a caller that
        is not the initiator.
    """

    task_id, note = _split_id(_command_args(ctx, args))
    if not task_id:
        return "用法：/nudge <id> [备注]"

    session_id = _session_of(ctx)
    view = _load_view(task_id)
    if view is None:
        return f"任务不存在：{task_id}"

    initiator = view.get("from_session")
    if not tools._is_privileged(session_id) and not _same_session(session_id, initiator):
        receiver = _one_line(view.get("to_session")) or "?"
        sender = _one_line(initiator) or "?"
        return (
            f"只有任务发起方可以催办：这个任务由 {sender} 发起、"
            f"{receiver} 执行，当前会话 {session_id} 无权催办。"
        )

    event: dict[str, Any] = {"type": "nudge", "value": note}
    if note:
        event["note"] = note
    try:
        views.append_event_once(task_id, event)
    except Exception as exc:  # noqa: BLE001 - a command must answer, not raise
        logger.warning("session-task: nudge failed for %s", task_id, exc_info=True)
        return f"催办失败：{exc}"

    state = _one_line(view.get("state")) or "?"
    if note:
        return f"已催办 {task_id}（当前状态 {state}）：{note}"
    return f"已催办 {task_id}（当前状态 {state}）。"


def _load_view(task_id: str) -> Optional[dict]:
    """Materialise an existing task, or ``None`` when the id is unknown.

    Unsafe ids (``../x``, ``nul``) raise from the store; they are caught here
    and answered as "not found", because a command handler's job is to reply
    in one line, not to hand the channel a traceback.
    """

    try:
        if not store.read_events(task_id):
            return None
        return views.materialize(task_id)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Host adapters
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - depends on the host installation
    from qwenpaw.app.runner.control_commands.base import (
        BaseControlCommandHandler as _HostCommandBase,
    )
except Exception:  # ImportError outside the host; any base is fine here
    _HostCommandBase = object  # type: ignore[assignment,misc]

#: Base class for the adapters. Outside the host this is ``object``: the
#: dispatcher only duck-types ``command_name`` / ``handle``, so a real
#: subclass is a nicety, not a requirement.
_CommandBase: Any = _HostCommandBase


class CommandName(str):
    """A control-command name that the host's two registries read differently.

    Stored slash-free (``"tasks"``) because that is what the bridge formats
    into a channel-registry key, and turned into ``"/tasks"`` by
    :meth:`lower` because that is what the dispatcher keys its handler
    registry with. See "Command contract" above for both host call sites.

    Deliberately a ``str``: everything that prints, compares or logs the name
    sees an ordinary string, and ``command_name == "tasks"`` is true. Only
    the one call the dispatch path makes — ``command_name.lower()`` — answers
    the slashed key, which is why this class cannot be a plain literal.
    """

    __slots__ = ()

    def lower(self) -> str:
        """``"/tasks"``: the key ``control_commands.register_command`` stores."""

        return "/" + str(self)


class _Adapter(_CommandBase):  # type: ignore[misc,valid-type]
    """Shared plumbing: take ``ControlContext``, call the pure handler."""

    command_name: Any = ""
    description = ""
    _handler: Any = None

    async def handle(self, context: Any) -> str:
        """Run the pure handler with the context's session and arguments."""

        return await self._handler(context, _args_of(context))


class TasksCommandHandler(_Adapter):
    """Host adapter for ``/tasks``.

    ``command_name`` is slash-free (:class:`CommandName`): the host prefixes
    it for the channel registry and ``lower()`` restores the slash for the
    dispatcher. Declaring ``"/tasks"`` here is what produced the ``//tasks``
    channel key and the TaskTracker detour (US-005 defect).
    """

    command_name = CommandName("tasks")
    description = "列出会话任务：待我处理 / 我发起的 / 已完成"
    _handler = staticmethod(handle_tasks)


class TaskCommandHandler(_Adapter):
    """Host adapter for ``/task <id>``."""

    command_name = CommandName("task")
    description = "查看单个会话任务的详情：/task <id>"
    _handler = staticmethod(handle_task)


class NudgeCommandHandler(_Adapter):
    """Host adapter for ``/nudge <id> [备注]`` (initiator only)."""

    command_name = CommandName("nudge")
    description = "催办一个任务（仅发起方）：/nudge <id> [备注]"
    _handler = staticmethod(handle_nudge)


#: Every handler ``plugin.register`` hands to ``register_control_command``.
COMMAND_HANDLERS: tuple[type, ...] = (
    TasksCommandHandler,
    TaskCommandHandler,
    NudgeCommandHandler,
)
