"""Tests for the six delegation tools and their registration (US-003).

Everything runs without the host: ``qwenpaw`` is faked through ``sys.modules``
where the session-injection path is exercised, and ``agentscope`` is absent
(``tools.ToolResponse is None``), which makes the tools return their plain
payload. One test installs a fake ``ToolResponse`` to prove the host path
still wraps the payload.

The API these tests encode was read from the installed QwenPaw 2.3 package,
not guessed:

* ``<workspace>\\QwenPaw\\Lib\\site-packages\\qwenpaw\\plugins\\loader.py`` — the entry
  module must export ``plugin`` and the loader calls ``plugin.register(api)``.
* ``<workspace>\\QwenPaw\\Lib\\site-packages\\qwenpaw\\plugins\\api.py`` —
  ``PluginApi.register_tool(tool_name, tool_func, description, icon, enabled)``.
* ``<workspace>\\QwenPaw\\Lib\\site-packages\\qwenpaw\\app\\agent_context.py`` —
  ``get_current_session_id()`` reads the ``ContextVar`` the runner sets, i.e.
  the host injection point for ``from_session``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest

import models
import plugin
import store
import tools
import views


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def plain_payloads(monkeypatch):
    """Force the tools to return raw payloads, independent of the host.

    ``agentscope`` is not installed in the test interpreter, but pinning both
    symbols makes the assertions hold even if it ever is.
    """

    monkeypatch.setattr(tools, "ToolResponse", None)
    monkeypatch.setattr(tools, "TextBlock", None)


@pytest.fixture()
def isolated_workspace(tmp_path, monkeypatch):
    """Point the store at a temp workspace, never at the real one."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture()
def origin_session(monkeypatch):
    """The session every tool call below is attributed to."""

    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")
    return "sess-a"


def _install_host_session(monkeypatch, session_id):
    """Fake the host ``ContextVar`` chain that the runner sets up.

    Mirrors ``qwenpaw.app.agent_context.get_current_session_id()``, which is
    what a tool sees while it runs inside a request.
    """

    root = types.ModuleType("qwenpaw")
    app = types.ModuleType("qwenpaw.app")
    agent_context = types.ModuleType("qwenpaw.app.agent_context")
    agent_context.get_current_session_id = lambda: session_id
    root.app = app
    app.agent_context = agent_context

    monkeypatch.setitem(sys.modules, "qwenpaw", root)
    monkeypatch.setitem(sys.modules, "qwenpaw.app", app)
    monkeypatch.setitem(sys.modules, "qwenpaw.app.agent_context", agent_context)


def _run(coro):
    """Run one tool call without pulling in an async pytest plugin."""

    return asyncio.run(coro)


def _add(to_session="sess-b", title="写一页说明", goal="产出一页 Markdown"):
    """Create a task through the tool under test and return its id."""

    task_id = _run(tools.task_add(to_session, title, goal))
    assert isinstance(task_id, str), task_id
    return task_id


def _create_task(from_session, to_session, title="直接造的任务"):
    """Write a ``created`` event straight to the log.

    Used where the test needs a task that the *current* session did not
    create (``task_list(role="to")``), which ``task_add`` cannot express
    because it always stamps the injected origin session.
    """

    task_id = store.new_task_id()
    views.append_event_once(
        task_id,
        {
            "type": "created",
            "title": title,
            "goal": "目标",
            "from_session": from_session,
            "to_session": to_session,
        },
    )
    return task_id


def _events(task_id):
    return store.read_events(task_id)


def _events_of_type(task_id, event_type):
    return [event for event in _events(task_id) if event.get("type") == event_type]


class FakeApi:
    """Records ``register_tool`` calls the way the host would make them."""

    def __init__(self):
        self.tools = []

    def register_tool(
        self,
        tool_name,
        tool_func,
        description="",
        icon="🔧",
        enabled=False,
    ):
        self.tools.append(
            {
                "tool_name": tool_name,
                "tool_func": tool_func,
                "description": description,
                "icon": icon,
                "enabled": enabled,
            }
        )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
EXPECTED_TOOLS = (
    "task_add",
    "task_list",
    "task_get",
    "task_report",
    "task_pulse",
    "task_cancel",
)

TRIGGER_WORDS = {
    "task_add": ("委派", "交给", "新建任务"),
    "task_list": ("任务列表", "进度", "清单"),
    "task_get": ("任务详情", "查看", "查任务"),
    "task_report": ("汇报", "完成", "更新状态"),
    "task_pulse": ("心跳", "还在做", "进度更新"),
    "task_cancel": ("取消", "撤销", "终止任务"),
}


def _registered_tools():
    """Run the plugin's registration against a recording API."""

    api = FakeApi()
    plugin.plugin.register(api)
    return api.tools


def test_plugin_registers_exactly_the_six_tools():
    api = FakeApi()
    plugin.plugin.register(api)

    names = [entry["tool_name"] for entry in api.tools]
    assert names == list(EXPECTED_TOOLS)
    assert [entry["tool_func"].__name__ for entry in api.tools] == list(EXPECTED_TOOLS)
    assert all(callable(entry["tool_func"]) for entry in api.tools)
    assert all(entry["enabled"] is True for entry in api.tools)
    assert all(entry["icon"] for entry in api.tools)


def test_registered_descriptions_are_chinese_and_mention_trigger_words():
    for entry in _registered_tools():
        description = entry["description"]
        assert re.search(r"[\u4e00-\u9fff]", description), description
        assert any(word in description for word in TRIGGER_WORDS[entry["tool_name"]]), (
            entry["tool_name"],
            description,
        )


def test_docstrings_are_chinese_and_mention_trigger_words():
    """The model sees the docstring, so the trigger words must live there too."""

    for entry in _registered_tools():
        doc = entry["tool_func"].__doc__ or ""
        assert re.search(r"[\u4e00-\u9fff]", doc), entry["tool_name"]
        assert any(word in doc for word in TRIGGER_WORDS[entry["tool_name"]]), (
            entry["tool_name"],
            doc.splitlines()[0] if doc else "",
        )


def test_every_tool_exposes_a_complete_parameter_schema():
    """Every parameter is typed and documented, so the JSON schema is filled."""

    for entry in _registered_tools():
        func = entry["tool_func"]
        signature = inspect.signature(func)
        doc = inspect.getdoc(func) or ""
        documented = {
            match.group(1)
            for match in re.finditer(r"^ {4}(\w+) \(", doc, re.MULTILINE)
        }

        assert signature.parameters, f"{entry['tool_name']} takes no parameters"
        for name, parameter in signature.parameters.items():
            assert parameter.annotation is not inspect.Parameter.empty, (
                f"{entry['tool_name']}.{name} has no type annotation"
            )
            assert name in documented, f"{entry['tool_name']}.{name} undocumented"

        # ``Returns:`` keeps the model oriented on the shape it gets back.
        assert "Returns:" in doc, entry["tool_name"]


def test_parameter_annotations_resolve_from_builtins_only():
    """Guard the host schema builder (regression for ``Optional[...]``).

    ``tools.py`` uses postponed annotations, so ``inspect.signature`` returns
    strings; the host evaluates them inside ``agentscope._utils._common``,
    whose namespace has no ``typing`` names. ``Optional[list[str]]`` therefore
    died with ``PydanticUserError`` at registration time while every bare
    test still passed. Annotations must need nothing but builtins.
    """

    for entry in _registered_tools():
        for name, parameter in inspect.signature(entry["tool_func"]).parameters.items():
            annotation = parameter.annotation
            assert isinstance(annotation, str), (entry["tool_name"], name, annotation)
            assert "Optional[" not in annotation, (entry["tool_name"], name)
            assert "typing" not in annotation, (entry["tool_name"], name)
            assert "Union[" not in annotation, (entry["tool_name"], name)


def test_from_session_is_not_a_parameter_of_any_tool():
    for entry in _registered_tools():
        parameters = inspect.signature(entry["tool_func"]).parameters
        assert "from_session" not in parameters, entry["tool_name"]
        assert "session_id" not in parameters, entry["tool_name"]


# --------------------------------------------------------------------------- #
# task_add
# --------------------------------------------------------------------------- #
def test_task_add_returns_a_task_id_and_records_the_origin_session(
    isolated_workspace, origin_session
):
    task_id = _run(tools.task_add("sess-b", "写报告", "产出 3 页"))

    assert isinstance(task_id, str) and task_id.startswith("t-")
    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.SUBMITTED
    assert view["from_session"] == origin_session
    assert view["to_session"] == "sess-b"
    assert view["title"] == "写报告"
    assert view["goal"] == "产出 3 页"
    assert view["artifacts"] == []
    assert _events_of_type(task_id, "created")


def test_task_add_records_requested_artifacts(isolated_workspace, origin_session):
    task_id = _run(
        tools.task_add("sess-b", "看图", "按图纸建模", ["a.md", " b.png ", ""])
    )

    assert views.materialize(task_id)["artifacts"] == ["a.md", "b.png"]


def test_task_add_accepts_a_bare_string_as_one_artifact(
    isolated_workspace, origin_session
):
    task_id = _run(tools.task_add("sess-b", "看图", "按图纸建模", "only.md"))

    assert views.materialize(task_id)["artifacts"] == ["only.md"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to_session": "", "title": "t", "goal": "g"},
        {"to_session": "  ", "title": "t", "goal": "g"},
        {"to_session": None, "title": "t", "goal": "g"},
        {"to_session": "sess-b", "title": "", "goal": "g"},
        {"to_session": "sess-b", "title": "t", "goal": "   "},
        {"to_session": "sess-b", "title": 42, "goal": "g"},
    ],
)
def test_task_add_rejects_blank_arguments(isolated_workspace, origin_session, kwargs):
    result = _run(tools.task_add(**kwargs))

    assert result["ok"] is False
    assert result["error"]
    assert not list(store.events_dir().glob("*.jsonl"))


# --------------------------------------------------------------------------- #
# task_list
# --------------------------------------------------------------------------- #
def test_task_list_returns_summaries_for_the_origin_session(
    isolated_workspace, origin_session
):
    first = _add(to_session="sess-b", title="任务一")
    second = _add(to_session="sess-b", title="任务二")
    _create_task("sess-c", "sess-c", title="别人的任务")  # unrelated to sess-a

    entries = _run(tools.task_list())

    assert isinstance(entries, list)
    ids = [entry["task_id"] for entry in entries]
    assert set(ids) == {first, second}
    assert all(
        set(entry) == {"task_id", "title", "state", "from", "to", "updated_at"}
        for entry in entries
    )


def test_task_list_filters_by_role(isolated_workspace, origin_session):
    outgoing = _add(to_session="sess-b", title="我派的")
    incoming = _create_task("sess-c", "sess-a", title="别人派的")

    from_entries = _run(tools.task_list(role="from"))
    to_entries = _run(tools.task_list(role="to"))
    all_entries = _run(tools.task_list(role="all"))

    assert outgoing in [entry["task_id"] for entry in from_entries]
    assert incoming not in [entry["task_id"] for entry in from_entries]
    assert incoming in [entry["task_id"] for entry in to_entries]
    assert {outgoing, incoming} <= {entry["task_id"] for entry in all_entries}


@pytest.mark.parametrize("role", ["inbox", "", None, "FROM"])
def test_task_list_rejects_an_unknown_role(isolated_workspace, origin_session, role):
    result = _run(tools.task_list(role=role))

    assert result["ok"] is False
    assert result["error"]


@pytest.mark.parametrize("limit", ["10", None, 1.5, True])
def test_task_list_rejects_a_non_integer_limit(
    isolated_workspace, origin_session, limit
):
    result = _run(tools.task_list(limit=limit))

    assert result["ok"] is False
    assert result["error"]


def test_task_list_limit_is_respected(isolated_workspace, origin_session):
    for index in range(3):
        _add(title=f"任务{index}")

    assert len(_run(tools.task_list(limit=2))) == 2
    assert _run(tools.task_list(limit=0)) == []


# --------------------------------------------------------------------------- #
# task_get
# --------------------------------------------------------------------------- #
def test_task_get_returns_the_full_view(isolated_workspace, origin_session):
    task_id = _add(to_session="sess-b", title="详情任务", goal="目标")

    view = _run(tools.task_get(task_id))

    assert view["task_id"] == task_id
    assert view["title"] == "详情任务"
    assert view["state"] == models.TaskState.SUBMITTED
    assert set(view) == set(views.VIEW_KEYS)


def test_task_get_rejects_an_unknown_task(isolated_workspace, origin_session):
    result = _run(tools.task_get("t-20260101-0000-deadbeef"))

    assert result["ok"] is False
    assert "不存在" in result["error"]


# --------------------------------------------------------------------------- #
# task_report
# --------------------------------------------------------------------------- #
def test_task_report_writes_state_summary_and_artifacts(
    isolated_workspace, origin_session
):
    task_id = _add()

    working = _run(tools.task_report(task_id, "working", "开始动手", ["out/draft.md"]))
    assert working == {"ok": True, "task_id": task_id, "state": "working"}

    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.WORKING
    assert view["summary"] == "开始动手"
    assert view["artifacts"] == ["out/draft.md"]

    completed = _run(
        tools.task_report(task_id, "completed", "写完了", ["out/final.md"])
    )
    assert completed == {"ok": True, "task_id": task_id, "state": "completed"}

    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.COMPLETED
    assert view["summary"] == "写完了"
    assert view["artifacts"] == ["out/final.md"]  # replaced, not appended


@pytest.mark.parametrize("state", ["submitted", "canceled", "done", "", None, 3])
def test_task_report_rejects_states_outside_the_report_set(
    isolated_workspace, origin_session, state
):
    task_id = _add()
    before = len(_events(task_id))

    result = _run(tools.task_report(task_id, state, "摘要"))

    assert result["ok"] is False
    assert "state" in result["error"]
    assert len(_events(task_id)) == before
    assert views.materialize(task_id)["state"] == models.TaskState.SUBMITTED


def test_task_report_rejects_an_illegal_transition(isolated_workspace, origin_session):
    """``submitted -> completed`` is not an edge in the state machine."""

    task_id = _add()
    before = len(_events(task_id))

    result = _run(tools.task_report(task_id, "completed", "跳步完成"))

    assert result["ok"] is False
    assert "illegal transition" in result["error"]
    assert len(_events(task_id)) == before


def test_task_report_rejects_an_empty_summary(isolated_workspace, origin_session):
    task_id = _add()

    result = _run(tools.task_report(task_id, "working", "   "))

    assert result["ok"] is False
    assert views.materialize(task_id)["state"] == models.TaskState.SUBMITTED


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_task_report_rejects_a_terminal_task(
    isolated_workspace, origin_session, terminal
):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    assert _run(tools.task_report(task_id, terminal, "收尾"))["ok"] is True

    before = len(_events(task_id))
    result = _run(tools.task_report(task_id, "working", "想重开"))

    assert result["ok"] is False
    assert "终态" in result["error"]
    assert len(_events(task_id)) == before
    assert views.materialize(task_id)["state"] == terminal


def test_task_report_is_idempotent_on_retry(isolated_workspace, origin_session):
    task_id = _add()

    first = _run(tools.task_report(task_id, "working", "同一份汇报", ["a.md"]))
    second = _run(tools.task_report(task_id, "working", "同一份汇报", ["a.md"]))

    assert first == second
    assert len(_events_of_type(task_id, "state")) == 1
    assert len(_events_of_type(task_id, "summary")) == 1
    assert len(_events_of_type(task_id, "artifacts")) == 1


# --------------------------------------------------------------------------- #
# task_pulse
# --------------------------------------------------------------------------- #
def test_task_pulse_appends_a_pulse_event(isolated_workspace, origin_session):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))

    result = _run(tools.task_pulse(task_id, "正在读需求"))

    assert result == {"ok": True, "task_id": task_id, "state": "working"}
    pulses = _events_of_type(task_id, "pulse")
    assert [event["value"] for event in pulses] == ["正在读需求"]
    assert views.materialize(task_id)["state"] == models.TaskState.WORKING


def test_task_pulse_is_idempotent_for_the_same_note(
    isolated_workspace, origin_session
):
    task_id = _add()

    _run(tools.task_pulse(task_id, "同一拍"))
    _run(tools.task_pulse(task_id, "同一拍"))
    _run(tools.task_pulse(task_id, "另一拍"))

    notes = [event["value"] for event in _events_of_type(task_id, "pulse")]
    assert notes == ["同一拍", "另一拍"]


def test_task_pulse_rejects_a_terminal_task(isolated_workspace, origin_session):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    _run(tools.task_report(task_id, "completed", "完成"))

    before = len(_events(task_id))
    result = _run(tools.task_pulse(task_id, "还在做"))

    assert result["ok"] is False
    assert "终态" in result["error"]
    assert len(_events(task_id)) == before


def test_task_pulse_rejects_an_unknown_task(isolated_workspace, origin_session):
    result = _run(tools.task_pulse("t-20260101-0000-deadbeef"))

    assert result["ok"] is False
    assert "不存在" in result["error"]


# --------------------------------------------------------------------------- #
# task_cancel
# --------------------------------------------------------------------------- #
def test_task_cancel_moves_to_canceled_and_records_the_reason(
    isolated_workspace, origin_session
):
    task_id = _add()

    result = _run(tools.task_cancel(task_id, "需求取消"))

    assert result == {"ok": True, "task_id": task_id, "state": "canceled"}
    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.CANCELED
    cancel_events = [
        event
        for event in _events_of_type(task_id, "state")
        if event.get("value") == models.TaskState.CANCELED
    ]
    assert cancel_events and cancel_events[0].get("note") == "需求取消"


@pytest.mark.parametrize("terminal", ["completed", "failed", "canceled"])
def test_task_cancel_rejects_a_terminal_task(
    isolated_workspace, origin_session, terminal
):
    task_id = _add()
    if terminal == "canceled":
        assert _run(tools.task_cancel(task_id, "先取消"))["ok"] is True
    else:
        _run(tools.task_report(task_id, "working", "开工"))
        _run(tools.task_report(task_id, terminal, "收尾"))

    before = len(_events(task_id))
    result = _run(tools.task_cancel(task_id, "再取消一次"))

    assert result["ok"] is False
    assert "终态" in result["error"]
    assert len(_events(task_id)) == before
    assert views.materialize(task_id)["state"] == terminal


# --------------------------------------------------------------------------- #
# Session injection
# --------------------------------------------------------------------------- #
def test_from_session_comes_from_the_host_context(
    isolated_workspace, monkeypatch, origin_session
):
    """The host ``ContextVar`` wins over the environment variable."""

    _install_host_session(monkeypatch, "sess-host")

    task_id = _run(tools.task_add("sess-b", "标题", "目标"))

    assert views.materialize(task_id)["from_session"] == "sess-host"
    assert _run(tools.task_list())[0]["task_id"] == task_id


def test_from_session_falls_back_to_the_env_then_a_literal(
    isolated_workspace, monkeypatch
):
    monkeypatch.delenv(tools.SESSION_ENV, raising=False)

    assert tools.resolve_session() == tools.FALLBACK_SESSION

    from_env = _run(tools.task_add("sess-b", "标题", "目标"))

    # The env is unset, so the task is attributed to the literal fallback
    # rather than being silently anonymous.
    assert views.materialize(from_env)["from_session"] == tools.FALLBACK_SESSION


def test_host_context_blank_string_falls_through_to_env(
    isolated_workspace, monkeypatch, origin_session
):
    _install_host_session(monkeypatch, "   ")

    assert tools.resolve_session() == origin_session


# --------------------------------------------------------------------------- #
# Robustness and host response wrapping
# --------------------------------------------------------------------------- #
def test_tools_never_raise_on_garbage_input(isolated_workspace, origin_session):
    assert _run(tools.task_get(123))["ok"] is False
    assert _run(tools.task_get("../escape"))["ok"] is False
    assert _run(tools.task_report(None, "working", "x"))["ok"] is False
    assert _run(tools.task_pulse(object()))["ok"] is False
    assert _run(tools.task_cancel(""))["ok"] is False
    assert _run(tools.task_add(object(), "t", "g"))["ok"] is False


def test_payload_is_wrapped_in_a_tool_response_when_the_host_provides_one(
    isolated_workspace, origin_session, monkeypatch
):
    class FakeTextBlock:
        def __init__(self, **kwargs):
            self.type = kwargs.get("type")
            self.text = kwargs.get("text")

    class FakeToolResponse:
        def __init__(self, content):
            self.content = content

    monkeypatch.setattr(tools, "TextBlock", FakeTextBlock)
    monkeypatch.setattr(tools, "ToolResponse", FakeToolResponse)

    task_id = _run(tools.task_add("sess-b", "标题", "目标"))
    assert isinstance(task_id, FakeToolResponse)
    assert task_id.content[0].type == "text"

    entries = _run(tools.task_list())
    assert isinstance(entries, FakeToolResponse)
    assert json.loads(entries.content[0].text)[0]["task_id"] == task_id.content[0].text

    # Error payloads are wrapped too, so the model sees the message as text.
    missing = _run(tools.task_get("t-20260101-0000-deadbeef"))
    assert isinstance(missing, FakeToolResponse)
    assert json.loads(missing.content[0].text)["ok"] is False


def test_plugin_json_declares_the_backend_entry():
    """The manifest must keep pointing at ``plugin.py`` (which now exists)."""

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "plugin.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["entry"]["backend"] == "plugin.py"
    assert (Path(__file__).resolve().parent.parent / "plugin.py").exists()


def test_plugin_json_declares_every_tool_in_meta_tools():
    """D8: the host resolves a tool back to its plugin through ``meta.tools``.

    ``qwenpaw.plugins.registry.PluginRegistry.get_plugin_id_for_tool`` and
    ``loader._cleanup_plugin_tools`` both read ``meta.tools`` as a list of
    dicts keyed by ``name``; with the field missing, uninstalling the plugin
    left its six functions behind in ``qwenpaw.agents.tools`` and the tool
    management UI could not attribute them.
    """

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "plugin.json").read_text(
            encoding="utf-8"
        )
    )

    tools = manifest["meta"]["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == list(EXPECTED_TOOLS)
    assert all(tool.get("description") for tool in tools)


# --------------------------------------------------------------------------- #
# D1 - authorization: only the origin and the receiver may touch a task
# --------------------------------------------------------------------------- #
def _as_session(monkeypatch, session_id):
    """Act as ``session_id`` for the next tool calls."""

    monkeypatch.setenv(tools.SESSION_ENV, session_id)
    return session_id


def _jsonl_count():
    return len(list(store.events_dir().glob("*.jsonl")))


def test_a_stranger_cannot_read_or_advance_a_task(
    isolated_workspace, origin_session, monkeypatch
):
    """Knowing a ``task_id`` is not authority: D1's reported break.

    Before this check a third session could cancel somebody else's task just
    by holding its id.
    """

    task_id = _add(to_session="sess-b", title="别人的任务", goal="不该被你碰")
    _run(tools.task_report(task_id, "working", "开工", ["out/draft.md"]))
    before = len(_events(task_id))

    _as_session(monkeypatch, "sess-stranger")

    for result in (
        _run(tools.task_get(task_id)),
        _run(tools.task_report(task_id, "completed", "我替你完成了")),
        _run(tools.task_pulse(task_id, "探针")),
        _run(tools.task_cancel(task_id, "我替你取消")),
    ):
        assert result == {"ok": False, "error": "无权访问该任务"}

    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.WORKING
    assert view["summary"] == "开工"
    assert len(_events(task_id)) == before


def test_a_stranger_sees_no_tasks_in_the_list(
    isolated_workspace, origin_session, monkeypatch
):
    task_id = _add(to_session="sess-b", title="别人的任务")

    _as_session(monkeypatch, "sess-stranger")

    assert _run(tools.task_list(role="all")) == []
    assert _run(tools.task_list(role="from")) == []
    assert _run(tools.task_list(role="to")) == []
    assert task_id not in [
        entry["task_id"] for entry in _run(tools.task_list(role="all"))
    ]


def test_the_origin_and_the_receiver_may_each_read_and_advance(
    isolated_workspace, origin_session, monkeypatch
):
    """PRD: 发起方与接收方都有权推进."""

    task_id = _add(to_session="sess-b", title="双方都能推进")

    # from_session
    assert _run(tools.task_get(task_id))["task_id"] == task_id
    assert _run(tools.task_report(task_id, "working", "发起方记一笔"))["ok"] is True

    # to_session
    _as_session(monkeypatch, "sess-b")
    assert _run(tools.task_get(task_id))["task_id"] == task_id
    assert _run(tools.task_pulse(task_id, "接收方心跳"))["ok"] is True
    assert _run(tools.task_report(task_id, "completed", "接收方做完"))["ok"] is True

    # from_session may still cancel a task it delegated
    _as_session(monkeypatch, "sess-a")
    second = _add(to_session="sess-b", title="发起方取消")
    assert _run(tools.task_cancel(second, "不需要了"))["state"] == "canceled"


def test_the_unknown_session_is_a_stranger_unless_the_switch_is_on(
    isolated_workspace, origin_session, monkeypatch
):
    """``unknown:unknown`` is only privileged behind ``QWENPAW_TASK_ALLOW_SYSTEM``."""

    task_id = _add(to_session="sess-b", title="系统会话不能默认进来")
    monkeypatch.delenv(tools.SESSION_ENV, raising=False)
    monkeypatch.delenv(tools.ALLOW_SYSTEM_ENV, raising=False)

    assert tools.resolve_session() == tools.FALLBACK_SESSION
    assert _run(tools.task_get(task_id)) == {"ok": False, "error": "无权访问该任务"}
    assert views.materialize(task_id)["state"] == models.TaskState.SUBMITTED

    monkeypatch.setenv(tools.ALLOW_SYSTEM_ENV, "1")

    assert _run(tools.task_get(task_id))["task_id"] == task_id
    assert _run(tools.task_report(task_id, "working", "系统推进"))["ok"] is True


def test_the_system_switch_does_not_help_a_named_stranger(
    isolated_workspace, origin_session, monkeypatch
):
    task_id = _add(to_session="sess-b", title="开关只对 unknown 生效")

    _as_session(monkeypatch, "sess-x")
    monkeypatch.setenv(tools.ALLOW_SYSTEM_ENV, "1")

    assert _run(tools.task_get(task_id)) == {"ok": False, "error": "无权访问该任务"}
    assert _run(tools.task_cancel(task_id, "顺手取消")) == {
        "ok": False,
        "error": "无权访问该任务",
    }


# --------------------------------------------------------------------------- #
# D2 - working -> working may refresh the summary
# --------------------------------------------------------------------------- #
def test_a_second_working_report_refreshes_the_summary_in_place(
    isolated_workspace, origin_session
):
    task_id = _add()
    assert (
        _run(tools.task_report(task_id, "working", "第一版", ["out/v1.md"]))["ok"]
        is True
    )
    states_before = len(_events_of_type(task_id, "state"))

    result = _run(tools.task_report(task_id, "working", "第二版：还在写", ["out/v2.md"]))

    assert result == {"ok": True, "task_id": task_id, "state": "working"}
    assert len(_events_of_type(task_id, "state")) == states_before

    view = views.materialize(task_id)
    assert view["state"] == models.TaskState.WORKING
    assert view["summary"] == "第二版：还在写"
    assert view["artifacts"] == ["out/v2.md"]


def test_a_working_refresh_still_refuses_a_finished_task(
    isolated_workspace, origin_session
):
    """D2 relaxes ``working -> working`` only; terminal stays frozen."""

    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    _run(tools.task_report(task_id, "completed", "完成"))
    before = len(_events(task_id))

    result = _run(tools.task_report(task_id, "working", "还在写"))

    assert result["ok"] is False
    assert "终态" in result["error"]
    assert len(_events(task_id)) == before


# --------------------------------------------------------------------------- #
# D3 - terminal retries are idempotent, not errors
# --------------------------------------------------------------------------- #
def test_retrying_the_terminal_report_is_idempotent(
    isolated_workspace, origin_session
):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    first = _run(tools.task_report(task_id, "completed", "写完了", ["out/final.md"]))
    before = len(_events(task_id))

    again = _run(tools.task_report(task_id, "completed", "写完了", ["out/final.md"]))

    assert first == again == {"ok": True, "task_id": task_id, "state": "completed"}
    assert len(_events(task_id)) == before


def test_a_different_report_on_a_finished_task_is_rejected(
    isolated_workspace, origin_session
):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    _run(tools.task_report(task_id, "completed", "写完了", ["out/final.md"]))
    before = len(_events(task_id))

    result = _run(tools.task_report(task_id, "completed", "换一个说法", ["out/final.md"]))

    assert result == {"ok": False, "error": "任务已完成，不能再修改"}
    assert len(_events(task_id)) == before
    assert views.materialize(task_id)["summary"] == "写完了"


def test_a_different_artifact_list_on_a_finished_task_is_rejected(
    isolated_workspace, origin_session
):
    task_id = _add()
    _run(tools.task_report(task_id, "working", "开工"))
    _run(tools.task_report(task_id, "completed", "写完了", ["out/final.md"]))

    result = _run(tools.task_report(task_id, "completed", "写完了", ["out/other.md"]))

    assert result["ok"] is False
    assert views.materialize(task_id)["artifacts"] == ["out/final.md"]


def test_retrying_the_same_cancel_is_idempotent(
    isolated_workspace, origin_session
):
    task_id = _add()
    first = _run(tools.task_cancel(task_id, "需求取消"))
    before = len(_events(task_id))

    again = _run(tools.task_cancel(task_id, "需求取消"))

    assert first == again == {"ok": True, "task_id": task_id, "state": "canceled"}
    assert len(_events(task_id)) == before


def test_a_second_cancel_with_a_different_reason_is_rejected(
    isolated_workspace, origin_session
):
    task_id = _add()
    _run(tools.task_cancel(task_id, "需求取消"))
    before = len(_events(task_id))

    result = _run(tools.task_cancel(task_id, "另一个理由"))

    assert result["ok"] is False
    assert "终态" in result["error"]
    assert len(_events(task_id)) == before


# --------------------------------------------------------------------------- #
# D4 - artifacts must be a list of strings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "artifacts",
    [{"a": 1}, 7, [1, 2], ["ok.md", 3], (b"raw",), [None]],
)
def test_task_report_rejects_non_string_artifacts(
    isolated_workspace, origin_session, artifacts
):
    task_id = _add()
    before = len(_events(task_id))

    result = _run(tools.task_report(task_id, "working", "摘要", artifacts))

    assert result == {"ok": False, "error": "artifacts 必须是字符串列表"}
    assert len(_events(task_id)) == before


@pytest.mark.parametrize("artifacts", [{"a": 1}, 7, [1, 2], [{"a": 1}]])
def test_task_add_rejects_non_string_artifacts(
    isolated_workspace, origin_session, artifacts
):
    result = _run(tools.task_add("sess-b", "标题", "目标", artifacts))

    assert result == {"ok": False, "error": "artifacts 必须是字符串列表"}
    assert _jsonl_count() == 0


def test_none_artifacts_means_an_empty_list(isolated_workspace, origin_session):
    task_id = _run(tools.task_add("sess-b", "标题", "目标", None))
    assert views.materialize(task_id)["artifacts"] == []

    assert _run(tools.task_report(task_id, "working", "开工", None))["ok"] is True
    assert views.materialize(task_id)["artifacts"] == []


# --------------------------------------------------------------------------- #
# D5 - note / reason must be strings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("note", [42, None, ["x"], {"a": 1}, b"raw"])
def test_task_pulse_rejects_a_non_string_note(
    isolated_workspace, origin_session, note
):
    task_id = _add()

    result = _run(tools.task_pulse(task_id, note))

    assert result == {"ok": False, "error": "note 必须是字符串"}
    assert not _events_of_type(task_id, "pulse")


@pytest.mark.parametrize("reason", [42, None, ["x"], 3.5])
def test_task_cancel_rejects_a_non_string_reason(
    isolated_workspace, origin_session, reason
):
    task_id = _add()

    result = _run(tools.task_cancel(task_id, reason))

    assert result == {"ok": False, "error": "reason 必须是字符串"}
    assert views.materialize(task_id)["state"] == models.TaskState.SUBMITTED


# --------------------------------------------------------------------------- #
# D6 - length budgets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "make_call, field",
    [
        (lambda task_id: tools.task_add("sess-b", "标" * (tools.MAX_TITLE + 1), "目标"), "title"),
        (lambda task_id: tools.task_add("sess-b", "标题", "目" * (tools.MAX_GOAL + 1)), "goal"),
        (
            lambda task_id: tools.task_report(
                task_id, "working", "摘" * (tools.MAX_SUMMARY + 1)
            ),
            "summary",
        ),
        (lambda task_id: tools.task_pulse(task_id, "拍" * (tools.MAX_NOTE + 1)), "note"),
        (lambda task_id: tools.task_cancel(task_id, "因" * (tools.MAX_REASON + 1)), "reason"),
        (
            lambda task_id: tools.task_add(
                "sess-b", "标题", "目标", ["a" * (tools.MAX_ARTIFACT_CHARS + 1)]
            ),
            "artifacts",
        ),
        (
            lambda task_id: tools.task_add(
                "sess-b",
                "标题",
                "目标",
                [f"f{index}.md" for index in range(tools.MAX_ARTIFACT_ITEMS + 1)],
            ),
            "artifacts",
        ),
    ],
)
def test_oversized_fields_are_rejected_before_any_write(
    isolated_workspace, origin_session, make_call, field
):
    task_id = _add()
    before = len(_events(task_id))
    files_before = _jsonl_count()

    result = _run(make_call(task_id))

    assert result["ok"] is False
    assert field in result["error"]
    assert "超过" in result["error"]
    assert len(_events(task_id)) == before
    assert _jsonl_count() == files_before


def test_fields_exactly_at_their_budget_are_accepted(
    isolated_workspace, origin_session
):
    task_id = _run(
        tools.task_add(
            "sess-b",
            "标" * tools.MAX_TITLE,
            "目" * tools.MAX_GOAL,
            ["a" * tools.MAX_ARTIFACT_CHARS],
        )
    )
    assert isinstance(task_id, str), task_id

    assert (
        _run(tools.task_report(task_id, "working", "摘" * tools.MAX_SUMMARY))["ok"]
        is True
    )
    assert _run(tools.task_pulse(task_id, "拍" * tools.MAX_NOTE))["ok"] is True
    assert _run(tools.task_cancel(task_id, "因" * tools.MAX_REASON))["ok"] is True


def test_task_list_truncates_a_long_title(isolated_workspace, origin_session):
    long_title = "标" * tools.MAX_TITLE
    task_id = _add(to_session="sess-b", title=long_title)

    entry = next(
        row for row in _run(tools.task_list()) if row["task_id"] == task_id
    )

    assert len(entry["title"]) == tools.LIST_TITLE_CHARS
    assert entry["title"].endswith("…")
    # Only the prompt-facing row is shortened; the log keeps the whole title.
    assert views.materialize(task_id)["title"] == long_title


def test_task_list_keeps_a_short_title_verbatim(isolated_workspace, origin_session):
    task_id = _add(to_session="sess-b", title="短标题")

    entry = next(
        row for row in _run(tools.task_list()) if row["task_id"] == task_id
    )

    assert entry["title"] == "短标题"


# --------------------------------------------------------------------------- #
# D7 - limit handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("limit", [-1, -50])
def test_task_list_rejects_a_negative_limit(isolated_workspace, origin_session, limit):
    result = _run(tools.task_list(limit=limit))

    assert result == {"ok": False, "error": "limit 必须是非负整数"}


def test_task_list_limit_zero_is_an_empty_list(isolated_workspace, origin_session):
    _add()

    assert _run(tools.task_list(limit=0)) == []


def test_task_list_clamps_a_limit_above_fifty(isolated_workspace, origin_session):
    for index in range(51):
        _create_task("sess-a", "sess-b", title=f"批量{index}")

    assert len(_run(tools.task_list(limit=51))) == 50
    assert len(_run(tools.task_list(limit=10_000))) == 50

