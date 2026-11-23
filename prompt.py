"""Prompt text the plugin injects into live sessions (US-003).

Two texts, both built from a materialised view (``views.materialize``):

* :func:`worker_task_card` — the brief handed to the session expected to *do*
  the task. It rides along in a normal prompt, so its length is budgeted: the
  title / initiator / goal are clipped, the five steps are fixed text, and the
  assembled card is clipped to :data:`MAX_CARD_CHARS`.
* :func:`initiator_summary` — the single line pushed back to the session that
  delegated the work: a state emoji, the title, the verb, and the first
  characters of the worker's own summary.

Why the card is a card and not a plan
-------------------------------------
The card names *the task* (``task_id``), *why* it matters (``goal``) and *how
to report* (the tool calls). It deliberately carries no acceptance criteria,
no story list and no other task's title: a worker handed the whole plan starts
editing the plan instead of doing the work, and every injected character is
paid for on every turn of that session. The numbers below are therefore part
of the contract, not cosmetics:

* :data:`MAX_CARD_CHARS` = 300 characters (CJK counts as 1, as users count);
* :data:`MAX_SUMMARY_CHARS` = 120 characters, always exactly one line —
  chat channels collapse multi-line bot messages, so a second line is noise.

Both numbers are asserted in ``tests/test_prompt.py``.
"""

from __future__ import annotations

from typing import Any

try:  # imported as part of the plugin package
    from .models import TaskState
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    from models import TaskState  # type: ignore[no-redef]

__all__ = [
    "CARD_STEP_LINES",
    "MAX_CARD_CHARS",
    "MAX_SUMMARY_CHARS",
    "initiator_summary",
    "worker_task_card",
]

#: Hard ceiling for :func:`worker_task_card`.
MAX_CARD_CHARS = 300

#: Hard ceiling for :func:`initiator_summary` (and it must stay one line).
MAX_SUMMARY_CHARS = 120

#: Per-field budgets inside the card. Together they keep the card under
#: :data:`MAX_CARD_CHARS` *before* the final clip, so a long title cannot push
#: the "you must report" sentence out of the text.
MAX_ID_CHARS = 32
MAX_TITLE_CHARS = 16
#: Wide enough for real ids: ``console:default`` is 15, ``wechat:wxid_...``
#: is longer but still recognisable when clipped.
MAX_FROM_CHARS = 20
MAX_GOAL_CHARS = 22
#: Floors used when the header has to shrink to keep the closing sentence.
MIN_TITLE_CHARS = 8
MIN_FROM_CHARS = 6
MIN_GOAL_CHARS = 8

#: Budget inside :func:`initiator_summary`.
MAX_SUMMARY_TITLE_CHARS = 40
MAX_SUMMARY_BODY_CHARS = 80
#: Never shrink the title below this when the body needs the room.
MIN_SUMMARY_TITLE_CHARS = 8

#: States with a dedicated one-liner: (emoji, verb, verb_when_body_follows).
#: ``canceled`` never carries the worker's summary — "已取消" is the whole
#: message, and the reason belongs to the initiator, not to the channel.
_VERBS = {
    TaskState.COMPLETED: ("✅", "完成", "完成："),
    TaskState.INPUT_REQUIRED: ("⏸", "需要你", "需要你："),
    TaskState.FAILED: ("❌", "失败", "失败："),
    TaskState.CANCELED: ("🚫", "已取消", "已取消"),
}

#: Prefix for every state that has no dedicated line (submitted / working).
IN_PROGRESS_PREFIX = "▶"

#: The five instruction lines. ``{task_id}`` is the only placeholder: the id
#: is stated once in the header as ``id=<task_id>`` and referenced by name
#: afterwards, because repeating a 19-character id five times costs ~95
#: characters of a 300-character budget.
CARD_STEP_LINES = (
    "1) task_get({task_id}) 看详情",
    "2) task_pulse({task_id}, '收到') 接单",
    "3) 边做边 task_report({task_id}, 'working', '进展')",
    "4) 完成 task_report({task_id}, 'completed', '结果', ['产物'])",
    "5) 缺信息 task_report({task_id}, 'input-required', '你要什么')",
)

#: Last line of the card. The single most expensive failure of a delegated
#: task is a worker that finishes silently, so this is not optional.
CARD_CLOSING = "做完必须 report，不能静默结束。"


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #
def _one_line(value: Any) -> str:
    """``value`` as a single stripped line (``""`` for anything unusable).

    Newlines, tabs and runs of spaces become one space: a title written as
    ``"a\\nb"`` must not break the card's layout or the summary's "one line"
    promise.
    """

    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = text.replace("\t", " ")
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking the cut with ``…``.

    The ellipsis counts towards the limit, so ``len(_clip(text, n)) <= n``
    holds for every ``n >= 0``. ``n == 0`` yields ``""``.
    """

    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _field(view: dict, key: str) -> Any:
    """``view[key]`` without assuming ``view`` is a dict."""

    if not isinstance(view, dict):
        return None
    return view.get(key)


# --------------------------------------------------------------------------- #
# Worker card
# --------------------------------------------------------------------------- #
def worker_task_card(view: dict) -> str:
    """The prompt section that hands one task to the worker session.

    Args:
        view: a materialised task view (``views.materialize``). Missing
            fields degrade to placeholders instead of raising: a card that
            cannot be built is a task nobody can pick up.

    Returns:
        str: at most :data:`MAX_CARD_CHARS` characters, several short lines.
        It contains the task id, the initiator, the goal, the five tool calls
        and the "you must report" sentence.
    """

    task_id = _clip(_one_line(_field(view, "task_id")), MAX_ID_CHARS) or "?"
    title = _one_line(_field(view, "title")) or "未命名任务"
    sender = _one_line(_field(view, "from_session")) or "未知会话"
    goal = _one_line(_field(view, "goal")) or "见任务详情"

    steps = "\n".join(step.format(task_id="id") for step in CARD_STEP_LINES)
    # 3 newlines: after the first header line, after the second, and after the
    # step block. Everything except the two header lines is fixed text, so the
    # header gets whatever is left — that is what keeps the closing sentence
    # inside the card no matter how long the title or the id is.
    header_room = MAX_CARD_CHARS - len(steps) - len(CARD_CLOSING) - 3
    head, tail = _card_header(task_id, title, sender, goal, header_room)

    lines = [head, tail, steps, CARD_CLOSING]
    return "\n".join(lines)


def _card_header(
    task_id: str,
    title: str,
    sender: str,
    goal: str,
    room: int,
) -> tuple[str, str]:
    """Fit the two header lines into ``room`` characters.

    Shrinking order is goal → title → sender. The goal is the least specific
    field (``task_get`` has the full text), the sender is nearly always short
    already, and the id is never shrunk: a truncated id is a wrong id.
    """

    title = _clip(title, MAX_TITLE_CHARS)
    sender = _clip(sender, MAX_FROM_CHARS)
    goal = _clip(goal, MAX_GOAL_CHARS)

    head = f"【任务 id={task_id}】{title}"
    tail = f"来自 {sender}｜目标 {goal}"

    for field in ("goal", "title", "sender"):
        overflow = len(head) + 1 + len(tail) - room
        if overflow <= 0:
            break
        if field == "goal":
            goal = _clip(goal, max(len(goal) - overflow, MIN_GOAL_CHARS))
        elif field == "title":
            title = _clip(title, max(len(title) - overflow, MIN_TITLE_CHARS))
        else:
            sender = _clip(sender, max(len(sender) - overflow, MIN_FROM_CHARS))
        head = f"【任务 id={task_id}】{title}"
        tail = f"来自 {sender}｜目标 {goal}"

    return head, tail


# --------------------------------------------------------------------------- #
# Initiator summary
# --------------------------------------------------------------------------- #
def _compose(prefix: str, title: str, label: str, body: str, suffix: str = "") -> str:
    """Join one summary line, shrinking the body and then the title to fit.

    Order matters: the summary text is what the initiator actually needs, so
    it gets the budget first and the title is shortened only if the body
    still does not fit. The final :func:`_clip` is the hard guarantee — every
    path returns at most :data:`MAX_SUMMARY_CHARS` characters.
    """

    title = _clip(_one_line(title) or "未命名任务", MAX_SUMMARY_TITLE_CHARS)
    body = _clip(_one_line(body), MAX_SUMMARY_BODY_CHARS)

    fixed = len(prefix) + len(title) + len(label) + len(suffix)
    room = MAX_SUMMARY_CHARS - fixed
    if body and len(body) > room:
        body = _clip(body, room)

    line = f"{prefix}{title}{label}{body}{suffix}"
    if len(line) > MAX_SUMMARY_CHARS:
        overflow = len(line) - MAX_SUMMARY_CHARS
        title = _clip(title, max(len(title) - overflow, MIN_SUMMARY_TITLE_CHARS))
        line = f"{prefix}{title}{label}{body}{suffix}"
    return _clip(line, MAX_SUMMARY_CHARS)


def initiator_summary(view: dict) -> str:
    """One line for the initiator's channel: state, title, and the result.

    Shapes (``<summary>`` is the worker's own text, clipped):

    * ``✅ <title> 完成：<summary>（N 个产物）``
    * ``⏸ <title> 需要你：<summary>``
    * ``❌ <title> 失败：<summary>``
    * ``🚫 <title> 已取消``
    * ``▶ <title> 进行中`` for every other state

    A missing summary degrades to the bare verb (``"✅ 写完了 完成"``) rather
    than to ``"完成：None"``. The result is always a single line of at most
    :data:`MAX_SUMMARY_CHARS` characters, because it is pushed through chat
    channels verbatim.
    """

    state = _field(view, "state")
    title = _one_line(_field(view, "title")) or "未命名任务"
    body = _one_line(_field(view, "summary"))

    if state == TaskState.CANCELED:
        return _compose("🚫 ", title, " 已取消", "")

    if state not in _VERBS:
        # submitted / working / an unknown value read back from a newer host:
        # "in progress" is the only honest line, and it never claims success.
        return _compose(IN_PROGRESS_PREFIX + " ", title, " 进行中", "")

    emoji, bare, with_body = _VERBS[state]
    suffix = ""
    if state == TaskState.COMPLETED:
        artifacts = _field(view, "artifacts")
        count = len(artifacts) if isinstance(artifacts, (list, tuple)) else 0
        if count:
            suffix = f"（{count} 个产物）"

    # No summary yet: keep the verb bare ("✅ x 完成") instead of dangling a
    # colon at the end of the line.
    middle = f" {with_body if body else bare}"
    return _compose(emoji + " ", title, middle, body, suffix)
