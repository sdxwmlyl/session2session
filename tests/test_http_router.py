"""Tests for the read-only HTTP surface (US-006).

Mounted exactly the way the host mounts it — ``/api`` + the plugin prefix —
against a real ``FastAPI`` app and ``TestClient``, so a route that only works
in isolation (wrong prefix, wrong path shape) fails here.

Covered: the list shape and its filters (``role`` / ``state`` / ``limit``),
the 50-row ceiling, the full ``materialize`` payload of the detail endpoint,
404 for an unknown id and 400 for an id the store would refuse.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import http_api
import models
import store
import views

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)

ME = "console:default"
OTHER = "console:worker"
FOREIGN = "console:stranger"
BASE = "/api/session-task"


class _AsyncASGIClient:
    """``.get(...)`` on an ASGI app without ``starlette.TestClient``.

    The Miniconda interpreter ships a starlette whose ``TestClient`` still
    forwards ``app=`` to ``httpx.Client`` — an argument httpx 0.28 removed —
    so constructing it raises ``TypeError`` there. This shim drives the very
    same ASGI app through ``httpx.ASGITransport``, which both httpx versions
    support; the request and the response object are unchanged.
    """

    def __init__(self, app) -> None:
        self._app = app

    def get(self, url: str, params=None):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.get(url, params=params)

        return asyncio.run(_run())


def make_client(app):
    """Prefer the real ``TestClient``; fall back on the version shim."""

    try:
        return TestClient(app)
    except TypeError:  # starlette/httpx version skew, not a plugin problem
        return _AsyncASGIClient(app)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Isolated event store: the host context must not leak in."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    # The host agent config is real on a developer machine; without this the
    # router's "probe every agent workspace" fallback would read live tasks.
    monkeypatch.setattr(store, "_host_agent_workspaces", lambda: {})
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture()
def agent_workspace(tmp_path):
    """A second store the process cwd/env cannot see, i.e. an agent workspace."""

    root = tmp_path / "agent-ws"
    (root / store.TASKS_DIRNAME / store.EVENTS_DIRNAME).mkdir(parents=True)
    return root


def make_task_in(root: Path, **kwargs) -> str:
    """Create a task inside another workspace root."""

    with store.workspace_scope(root):
        return make_task(**kwargs)


@pytest.fixture()
def client(workspace):
    app = fastapi.FastAPI()
    app.include_router(http_api.build_router(), prefix=BASE)
    return make_client(app)


def make_task(
    *,
    title: str = "整理回款明细",
    from_session: str = ME,
    to_session: str = OTHER,
    states: tuple[str, ...] = (),
    at: str | None = None,
) -> str:
    """Create one task through the real event store.

    ``at`` pins the timestamps so a "newest N" window is deterministic
    (``at`` has second resolution; a loop without it ties).
    """

    task_id = store.new_task_id()
    created = {
        "type": "created",
        "title": title,
        "goal": "导出并核对",
        "from_session": from_session,
        "to_session": to_session,
    }
    if at:
        created["at"] = at
    views.append_event_once(task_id, created)
    for state in states:
        views.append_state(task_id, state, **({"at": at} if at else {}))
    return task_id


# --------------------------------------------------------------------------- #
# GET /tasks
# --------------------------------------------------------------------------- #
def test_list_returns_the_public_row_shape(client):
    task_id = make_task(states=("working",))

    response = client.get(f"{BASE}/tasks", params={"session_id": ME})

    assert response.status_code == 200
    rows = response.json()
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert set(rows[0]) == {"id", "state", "title", "from", "to", "updated_at"}
    assert rows[0]["id"] == task_id
    assert rows[0]["state"] == models.TaskState.WORKING
    assert rows[0]["title"] == "整理回款明细"
    assert rows[0]["from"] == ME
    assert rows[0]["to"] == OTHER
    assert rows[0]["updated_at"]


def test_list_is_empty_for_a_session_without_tasks(client):
    make_task()

    response = client.get(f"{BASE}/tasks", params={"session_id": FOREIGN})

    assert response.status_code == 200
    assert response.json() == []


def test_list_filters_by_role(client):
    outgoing = make_task(title="我派出去的")
    incoming = make_task(title="派给我的", from_session=OTHER, to_session=ME)

    from_rows = client.get(
        f"{BASE}/tasks", params={"session_id": ME, "role": "from"}
    ).json()
    to_rows = client.get(
        f"{BASE}/tasks", params={"session_id": ME, "role": "to"}
    ).json()
    all_rows = client.get(
        f"{BASE}/tasks", params={"session_id": ME, "role": "all"}
    ).json()

    assert [row["id"] for row in from_rows] == [outgoing]
    assert [row["id"] for row in to_rows] == [incoming]
    assert {row["id"] for row in all_rows} == {outgoing, incoming}


def test_list_filters_by_state(client):
    working = make_task(title="在做", states=("working",))
    make_task(title="还没开始")

    response = client.get(
        f"{BASE}/tasks", params={"session_id": ME, "state": "working"}
    )

    assert [row["id"] for row in response.json()] == [working]


def test_state_filter_reaches_a_task_outside_the_newest_fifty(client):
    """US-006 defect: ``?state=`` was applied to an already capped page.

    One finished task, then 50 newer unfinished ones: the finished task is
    outside any "newest 50" window, and filtering afterwards returned nothing.
    Filtering runs first now, so the response is exactly that task.
    """

    finished = make_task(
        title="早就做完的",
        states=("working", "completed"),
        at="2026-11-22T09:00:00+08:00",
    )
    for index in range(views.SUMMARY_LIMIT):
        make_task(title=f"还没开始 {index}", at=f"2026-11-22T10:{index:02d}:00+08:00")

    response = client.get(
        f"{BASE}/tasks",
        params={"session_id": ME, "state": "completed", "limit": 50},
    )

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [finished]


def test_state_filter_combines_with_role_and_limit(client):
    incoming_done = make_task(
        title="别人给我、已做完",
        from_session=OTHER,
        to_session=ME,
        states=("working", "completed"),
        at="2026-11-22T09:00:00+08:00",
    )
    make_task(
        title="我自己发起的、已做完",
        states=("working", "completed"),
        at="2026-11-22T09:01:00+08:00",
    )

    rows = client.get(
        f"{BASE}/tasks",
        params={"session_id": ME, "role": "to", "state": "completed", "limit": 1},
    ).json()

    assert [row["id"] for row in rows] == [incoming_done]
    assert rows[0]["to"] == ME


def test_list_honours_the_limit(client):
    created = [make_task(title=f"任务 {index}") for index in range(4)]

    rows = client.get(f"{BASE}/tasks", params={"session_id": ME, "limit": 2}).json()

    assert len(rows) == 2
    assert {row["id"] for row in rows} <= set(created)


def test_list_caps_at_fifty_rows_whatever_the_caller_asks(client):
    for index in range(http_api.MAX_LIMIT + 5):
        make_task(title=f"任务 {index}")

    rows = client.get(f"{BASE}/tasks", params={"session_id": ME, "limit": 999}).json()

    assert len(rows) == http_api.MAX_LIMIT


def test_list_accepts_limit_zero_as_an_empty_page(client):
    make_task()

    rows = client.get(f"{BASE}/tasks", params={"session_id": ME, "limit": 0}).json()

    assert rows == []


@pytest.mark.parametrize(
    "params",
    [
        {"session_id": ME, "role": "sideways"},
        {"session_id": ME, "state": "almost-done"},
        {"session_id": ME, "limit": -1},
    ],
)
def test_list_rejects_bad_filters_with_400(client, params):
    response = client.get(f"{BASE}/tasks", params=params)

    assert response.status_code == 400


def test_list_requires_a_session_id(client):
    response = client.get(f"{BASE}/tasks")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# GET /tasks/{task_id}
# --------------------------------------------------------------------------- #
def test_detail_returns_the_whole_materialised_view(client):
    task_id = make_task(states=("working",))
    views.append_event_once(task_id, {"type": "summary", "value": "第一段完成"})

    response = client.get(f"{BASE}/tasks/{task_id}")

    assert response.status_code == 200
    view = response.json()
    assert set(view) == set(views.VIEW_KEYS)
    assert view["task_id"] == task_id
    assert view["state"] == models.TaskState.WORKING
    assert view["summary"] == "第一段完成"
    assert [item["type"] for item in view["history"]] == [
        "created",
        "state",
        "summary",
    ]


def test_detail_returns_404_for_an_unknown_task(client):
    response = client.get(f"{BASE}/tasks/t-20260101-0000-deadbeef")

    assert response.status_code == 404
    assert "任务不存在" in response.json()["detail"]


@pytest.mark.parametrize("bad_id", ["nul", "a*b"])
def test_detail_returns_400_for_an_illegal_id(client, bad_id):
    response = client.get(f"{BASE}/tasks/{bad_id}")

    assert response.status_code == 400


def test_detail_rejects_path_traversal(client):
    response = client.get(f"{BASE}/tasks/{'..%2F..%2Fetc%2Fpasswd'}")

    assert response.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# Workspace resolution (US-006 defect: a correct route that returned [])
# --------------------------------------------------------------------------- #
def test_list_resolves_the_workspace_from_the_agent_config(
    client, agent_workspace, monkeypatch
):
    """The host knows the agent's workspace even though the request has none.

    ``console:*`` requests run without an agent in the context var, so
    ``workspace_root()`` alone would look in the host process cwd. An
    ``agent/session`` id lets the router recover the configured workspace.
    """

    task_id = make_task_in(agent_workspace)
    monkeypatch.setattr(
        store, "_host_agent_workspaces", lambda: {"default": agent_workspace}
    )

    response = client.get(f"{BASE}/tasks", params={"session_id": "default:console:default"})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [task_id]


def test_list_falls_back_to_another_agent_workspace(
    client, workspace, agent_workspace, monkeypatch
):
    """No agent in the session id: the task log itself decides."""

    # A task in the local store, but for another session: the first candidate
    # root has data and still yields nothing, so the probe must continue.
    make_task(title="别人之间的事", from_session=OTHER, to_session=FOREIGN)
    anchored = make_task_in(agent_workspace, title="在 agent workspace 的")
    monkeypatch.setattr(
        store, "_host_agent_workspaces", lambda: {"other": agent_workspace}
    )

    rows = client.get(f"{BASE}/tasks", params={"session_id": ME}).json()

    assert [row["id"] for row in rows] == [anchored]


def test_list_honours_an_explicit_workspace_parameter(client, agent_workspace):
    task_id = make_task_in(agent_workspace)

    response = client.get(
        f"{BASE}/tasks",
        params={"session_id": ME, "workspace": str(agent_workspace)},
    )

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [task_id]


def test_detail_honours_an_explicit_workspace_parameter(client, agent_workspace):
    task_id = make_task_in(agent_workspace, states=("working", "completed"))

    response = client.get(
        f"{BASE}/tasks/{task_id}", params={"workspace": str(agent_workspace)}
    )

    assert response.status_code == 200
    assert response.json()["state"] == models.TaskState.COMPLETED


@pytest.mark.parametrize("shape", ["relative", "missing", "no-session-tasks"])
def test_bad_workspace_parameters_are_rejected_with_400(
    client, tmp_path, monkeypatch, shape
):
    if shape == "relative":
        value = "relative/path"
    elif shape == "missing":
        value = str(tmp_path / "not-here")
    else:
        empty = tmp_path / "plain-dir"
        empty.mkdir()
        value = str(empty)

    for url in (f"{BASE}/tasks", f"{BASE}/tasks/t-20260101-0000-deadbeef"):
        response = client.get(url, params={"session_id": ME, "workspace": value})
        assert response.status_code == 400, url


def test_list_warns_when_no_workspace_holds_the_session(client, caplog):
    """A silent ``[]`` was the defect: log the roots that were probed."""

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        response = client.get(f"{BASE}/tasks", params={"session_id": FOREIGN})

    assert response.status_code == 200
    assert response.json() == []
    hits = [r for r in caplog.records if "found no tasks for session" in r.getMessage()]
    assert len(hits) == 1
    assert FOREIGN in hits[0].getMessage()


def test_detail_warns_when_no_workspace_holds_the_task(client, caplog):
    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        response = client.get(f"{BASE}/tasks/t-20260101-0000-deadbeef")

    assert response.status_code == 404
    hits = [r for r in caplog.records if "found no tasks for session" in r.getMessage()]
    assert len(hits) == 1



# --------------------------------------------------------------------------- #
# Canonical task id — F2
# --------------------------------------------------------------------------- #
def test_detail_echoes_the_task_id_as_spelled_on_disk(client):
    """A case-insensitive hit answers the canonical id, not the request."""

    task_id = make_task()
    response = client.get(BASE + "/tasks/" + task_id.upper())

    assert response.status_code == 200
    assert response.json()["task_id"] == task_id


def test_detail_still_404s_for_an_unknown_id_in_another_case(client):
    response = client.get(BASE + "/tasks/T-20261122-2115-DEADBEEF")

    assert response.status_code == 404
