"""Read-only HTTP surface for session tasks (US-006).

Two endpoints, mounted by the host at ``/api/session-task`` (the registry
prefixes every plugin router with ``/api``; see
``qwenpaw/plugins/registry.py:register_http_router``)::

    GET /api/session-task/tasks?session_id=...&role=...&state=...&limit=...
    GET /api/session-task/tasks/{task_id}

Both accept an optional ``?workspace=<absolute path>`` naming the task store,
because a host HTTP request carries no agent in its context (see below).

Design notes
------------
* **Workspace is resolved, never assumed.** ``store.workspace_root()`` inside
  a request falls back to the host process cwd (``C:/Users/<user>/.copaw`` on
  Windows), which is *not* where the agent tools write. Every request
  therefore probes :func:`store.candidate_roots` — the agent's configured
  workspace when the session names one, then ``QWENPAW_WORKING_DIR``, then the
  old root, then each configured agent workspace — and reads whichever root
  actually holds the task log (US-006 defect: a correct route returning ``[]``).
* **Read-only.** Writes stay in the agent tools, where the caller's session is
  known from the host context variable. An HTTP writer would have to trust a
  query parameter for identity, and a reminder ("nudge") or a completion
  written by "whoever knew the id" is exactly the hole US-004 closed.
* **Same view layer as everything else.** The list is
  ``views.iter_session_views`` — session/role/state filtered, then sorted,
  then capped — so ``?state=completed`` finds a task that a "newest 50"
  window would have pushed out, and the detail is ``views.materialize``
  untouched, so a client sees exactly what ``task_get`` sees.
* **The list row is a fixed six-key shape** — ``id`` / ``state`` / ``title``
  / ``from`` / ``to`` / ``updated_at`` — and the shape is pinned in
  ``tests/test_http_router.py`` so a rename in ``views`` cannot silently
  change the API.
* **``limit`` is clamped, never trusted**: default 50, ceiling
  :data:`MAX_LIMIT` = 50 (the same ceiling ``views.SUMMARY_LIMIT`` applies, so
  a caller asking for 500 still gets at most 50 rows in one response).
* Errors follow FastAPI's convention: 404 for an id with no event log, 400 for
  an id the store would refuse as unsafe (``../x``, ``nul``), an unknown
  ``role``/``state``, or a negative ``limit``.

``build_router()`` imports FastAPI lazily so this module stays importable in
an interpreter without it (the plugin must never fail to load just because a
dependency is missing; ``plugin.register`` checks the API first).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

try:  # imported as part of the plugin package (host loader)
    from . import models, store, views
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    import models  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]
    import views  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "ROUTE_PREFIX",
    "TASK_ID_PATTERN",
    "build_router",
]

#: Prefix the host mounts this router under (``/api`` + this).
ROUTE_PREFIX = "/session-task"

#: Page size used when the caller does not ask for one.
DEFAULT_LIMIT = 50
#: Hard ceiling, identical to ``views.SUMMARY_LIMIT``: one response never
#: carries more rows than one prompt section would.
MAX_LIMIT = views.SUMMARY_LIMIT

#: Characters a task id may contain (it is a file name — see
#: ``store._TASK_ID_SAFE``). Reserved device names (``nul``, ``com1``) also
#: match this pattern and are rejected by the store itself, which is what the
#: 400 branch relies on.
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _row(entry: dict) -> dict:
    """One list row: the six public keys, nothing else."""

    return {
        "id": entry.get("task_id"),
        "state": entry.get("state"),
        "title": entry.get("title"),
        "from": entry.get("from"),
        "to": entry.get("to"),
        "updated_at": entry.get("updated_at"),
    }


def _checked_workspace(value: Optional[str]) -> Optional[Path]:
    """Validate an explicit ``?workspace=`` override.

    The parameter is an operator escape hatch (a task store the host cannot
    guess), not a way to read arbitrary directories: it must be an absolute
    path that already holds a ``session-tasks`` directory. Anything else is a
    400 instead of a confusing empty list.
    """

    if value is None:
        return None
    if not value.strip():
        raise ValueError("workspace 不能为空")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"workspace 必须是绝对路径：{value}")
    if not path.is_dir():
        raise ValueError(f"workspace 不存在或不是目录：{value}")
    if not (path / store.TASKS_DIRNAME).is_dir():
        raise ValueError(f"workspace 下没有 {store.TASKS_DIRNAME} 目录：{value}")
    return path


def _canonical_task_id(task_id: str, events_dir: Path) -> str:
    """The id exactly as the file on disk spells it (else ``task_id``).

    Windows resolves ``T-2026….jsonl`` case-insensitively, so
    ``GET /tasks/T-2026…`` used to answer 200 while echoing the *requested*
    spelling; a POSIX host answered 404 for the same request. A task id in a
    response is an identifier, not an echo of the request, so the name comes
    from the file that was actually read. Only consulted once the route has a
    hit (someone else's file is never renamed or created here).
    """

    wanted = task_id.lower()
    try:
        for path in events_dir.glob("*.jsonl"):
            if path.stem.lower() == wanted:
                return path.stem
    except OSError:  # pragma: no cover - unreadable directory
        logger.debug("could not list %s", events_dir, exc_info=True)
    return task_id


def _warn_unmatched(where: str, session_id: Optional[str], roots: list[Path]) -> None:
    """One warning per request: the roots probed and why nothing matched."""

    logger.warning(
        "session-task %s found no tasks for session %r; probed workspaces: %s "
        "(hint: pass ?workspace=<abs path containing %s>)",
        where,
        session_id,
        ", ".join(str(root) for root in roots) or "(none)",
        store.TASKS_DIRNAME,
    )


def build_router() -> Any:
    """Build the ``APIRouter`` for ``GET /api/session-task/tasks...``.

    Returns:
        fastapi.APIRouter: two read-only routes.

    Raises:
        ImportError: when FastAPI is not installed (the caller logs and skips
            HTTP instead of failing the plugin).
    """

    from fastapi import APIRouter, HTTPException, Query

    router = APIRouter()

    @router.get("/tasks")
    def list_tasks(
        session_id: str = Query(..., min_length=1, description="会话 ID"),
        role: str = Query("all", description="from / to / all"),
        state: Optional[str] = Query(
            None, description="按状态过滤：submitted / working / ..."
        ),
        limit: int = Query(DEFAULT_LIMIT, description="最多返回多少条，上限 50"),
        workspace: Optional[str] = Query(
            None,
            description=(
                "可选：任务库所在的绝对路径（须含 session-tasks 目录）。"
                "宿主 HTTP 上下文没有 agent，必要时用它指定 workspace。"
            ),
        ),
    ) -> list:
        """Task summaries for one session, newest first.

        ``session_id`` / ``role`` / ``state`` filter the **whole** session
        before ``limit`` truncates: filtering a 50-row window instead used to
        hide older matching tasks (US-006 defect).

        The store root is resolved per session (agent config → env → cwd), and
        every configured agent workspace is probed as a last resort: an HTTP
        request has no agent in context, so the plain ``workspace_root()``
        would be the host process cwd and could never see the agent's tasks.
        """

        if role not in views.ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"role 只能是 {' / '.join(views.ROLES)}",
            )
        if state is not None and state not in models.ALL_STATES:
            raise HTTPException(status_code=400, detail=f"未知状态：{state}")
        if limit < 0:
            raise HTTPException(status_code=400, detail="limit 必须是非负整数")

        try:
            explicit = _checked_workspace(workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        roots = [explicit] if explicit is not None else store.candidate_roots(session_id)
        size = min(limit, MAX_LIMIT)
        probed: list[Path] = []

        for root in roots:
            if explicit is None and not store.has_events_at(root):
                continue  # nothing was ever written here: do not materialise
            probed.append(root)
            with store.workspace_scope(root):
                try:
                    entries = views.iter_session_views(
                        session_id,
                        role=role,
                        states=None if state is None else (state,),
                        limit=size,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            if entries:
                return [_row(entry) for entry in entries]

        if explicit is None:
            _warn_unmatched("GET /tasks", session_id, probed or roots)
        return []

    @router.get("/tasks/{task_id}")
    def get_task(
        task_id: str,
        workspace: Optional[str] = Query(
            None,
            description=(
                "可选：任务库所在的绝对路径（须含 session-tasks 目录）。"
                "多 workspace 时用来指定去哪个库里找。"
            ),
        ),
    ) -> dict:
        """The full materialised view (history included) of one task."""

        if not TASK_ID_PATTERN.match(task_id):
            raise HTTPException(status_code=400, detail=f"非法 task_id：{task_id}")
        try:
            store.validate_task_id(task_id)  # reserved device names (nul, com1)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            explicit = _checked_workspace(workspace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        roots = [explicit] if explicit is not None else store.candidate_roots()
        probed: list[Path] = []

        for root in roots:
            if explicit is None and not store.has_events_at(root):
                continue
            probed.append(root)
            with store.workspace_scope(root):
                try:
                    events = store.read_events(task_id)
                except ValueError as exc:  # reserved device name, unsafe chars
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if not events:
                    continue
                canonical = _canonical_task_id(task_id, store.events_dir_at(root))
                return views.materialize(canonical)

        if explicit is None:
            _warn_unmatched("GET /tasks/{task_id}", None, probed or roots)
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    return router
