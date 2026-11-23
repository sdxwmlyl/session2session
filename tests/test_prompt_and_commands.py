"""Tests for the prompt section and slash commands (US-005).

What is pinned here:

* the digest injects **only** when the session has an active task, lists
  ``id | 标题 | 状态 | 最近心跳``, clips titles to 40 characters, shows at most
  five lines, and never exceeds 600 characters — even when the budget is
  squeezed;
* ``/tasks`` answers in the three groups (to me / by me / finished) and falls
  back to one hint line when the session has nothing;
* ``/nudge`` is initiator-only, writes a ``nudge`` event that carries the
  note, and does **not** move the state;
* ``plugin.register`` degrades instead of crashing when a host surface is
  missing, and passes the expected arguments when it is present (FakeApi).

The workspace is a temp directory: ``store._host_workspace_dir`` is
monkeypatched to ``None`` so the host package (present under the host
interpreter) cannot redirect the event store at the real workspace.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

import models
import plugin as plugin_module
import prompt_section
import store
import tools
import views


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Isolated event store, no host session, no ``task_add`` shortcut."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    monkeypatch.delenv(tools.SESSION_ENV, raising=False)
    return tmp_path


ME = "console:default"
OTHER = "console:worker"
FOREIGN = "console:stranger"


def make_task(
    *,
    title: str = "整理 11 月客户回款明细",
    from_session: str = ME,
    to_session: str = OTHER,
    states: tuple[str, ...] = (),
    pulse_age_seconds: float | None = None,
    note: str = "",
    at: str | None = None,
) -> str:
    """Create one task through the real event store, optionally pulsed.

    ``at`` pins every event to one ISO timestamp (e.g.
    ``"2026-11-22T10:03:00+08:00"``). The D1 scenarios need a *deterministic*
    order between a few dozen tasks, and ``at`` has second resolution, so
    creating them in a loop without it would leave the order to the tie-break.
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
    if pulse_age_seconds is not None:
        pulse_at = (
            datetime.now().astimezone() - timedelta(seconds=pulse_age_seconds)
        ).isoformat(timespec="seconds")
        store.append_event(task_id, {"type": "pulse", "value": note, "at": pulse_at})
    for state in states:
        views.append_state(task_id, state, **({"at": at} if at else {}))
    return task_id


def run(coro):
    """Drive one coroutine without requiring pytest-asyncio."""

    return asyncio.run(coro)


def command(ctx_session: str, args=""):
    """A ``ControlContext`` stand-in: only ``session_id`` / ``args`` are used."""

    return {"session_id": ctx_session, "args": args}


def nudge_events(task_id: str) -> list[dict]:
    return [
        event for event in store.read_events(task_id) if event.get("type") == "nudge"
    ]


# --------------------------------------------------------------------------- #
# digest()
# --------------------------------------------------------------------------- #
def test_digest_is_empty_when_the_session_has_no_task(workspace):
    assert prompt_section.digest(ME) == ""


def test_digest_is_empty_for_an_unknown_session(workspace):
    make_task()

    assert prompt_section.digest("console:nobody") == ""


def test_digest_lists_active_tasks_and_skips_finished_and_foreign_ones(workspace):
    mine = make_task()
    incoming = make_task(title="帮我查一份合同", from_session=OTHER, to_session=ME)
    finished = make_task(states=("working", "completed"))
    foreign = make_task(from_session=FOREIGN, to_session="console:elsewhere")

    text = prompt_section.digest(ME)

    assert mine in text
    assert incoming in text
    assert finished not in text
    assert foreign not in text
    assert "2 个进行中" in text


def test_digest_line_has_four_pipe_separated_fields_and_a_clipped_title(workspace):
    long_title = "很长的标题" * 40
    make_task(title=long_title, pulse_age_seconds=200)

    text = prompt_section.digest(ME)
    lines = [line for line in text.splitlines() if " | " in line]

    assert len(lines) == 1
    fields = lines[0].split(" | ")
    assert len(fields) == 4
    assert fields[0].startswith("t-")
    assert len(fields[1]) <= prompt_section.MAX_DIGEST_TITLE_CHARS
    assert fields[1].endswith("…")
    assert fields[2] == models.TaskState.SUBMITTED
    assert fields[3] == "3 分钟前"


def test_digest_says_never_pulsed_when_there_is_no_pulse(workspace):
    make_task()

    line = [line for line in prompt_section.digest(ME).splitlines() if " | " in line][0]

    assert line.endswith("未打点")


def test_digest_shows_at_most_five_lines(workspace):
    ids = [make_task(title=f"任务 {index}") for index in range(8)]

    text = prompt_section.digest(ME)
    shown = [line for line in text.splitlines() if " | " in line]

    assert len(shown) == prompt_section.MAX_DIGEST_LINES
    assert "8 个进行中" in text
    assert sum(1 for task_id in ids if task_id in text) == (
        prompt_section.MAX_DIGEST_LINES
    )


def test_digest_stays_within_the_character_budget(workspace):
    for index in range(8):
        make_task(title=f"{index} " + "超长标题" * 50, pulse_age_seconds=90)

    text = prompt_section.digest(ME)

    assert len(text) <= prompt_section.MAX_DIGEST_CHARS


def test_digest_drops_lines_then_clips_when_the_budget_is_tiny(workspace, monkeypatch):
    for index in range(5):
        make_task(title=f"{index} " + "超长标题" * 50, pulse_age_seconds=90)

    monkeypatch.setattr(prompt_section, "MAX_DIGEST_CHARS", 120)

    text = prompt_section.digest(ME)

    assert len(text) <= 120


def test_digest_rejects_a_bad_line_budget(workspace):
    with pytest.raises(ValueError):
        prompt_section.digest(ME, max_lines=0)


# --------------------------------------------------------------------------- #
# D1: the window must not hide an older active task
# --------------------------------------------------------------------------- #
def _active_task_behind_fifty_finished_ones() -> str:
    """One old active task, then 50 newer finished ones (the reported case)."""

    old = make_task(title="老任务还没做完", at="2026-11-22T09:00:00+08:00")
    for index in range(50):
        make_task(
            title=f"已完成 {index}",
            states=("working", "completed"),
            at=f"2026-11-22T10:{index:02d}:00+08:00",
        )
    return old


def test_digest_finds_an_active_task_behind_fifty_finished_ones(workspace):
    """US-005 defect: digest was built from the newest 50 tasks, then filtered.

    Fifty freshly completed tasks therefore made the section empty even
    though the session had an active task. The scan now filters by state
    before sorting and capping.
    """

    old = _active_task_behind_fifty_finished_ones()

    text = prompt_section.digest(ME)

    assert text, "digest must not be empty while an active task exists"
    assert old in text
    assert "1 个进行中" in text
    assert "已完成 49" not in text


def test_tasks_lists_an_active_task_behind_fifty_finished_ones(workspace):
    """Same defect on the ``/tasks`` surface: it printed 待我处理（0）/我发起的（0）."""

    old = _active_task_behind_fifty_finished_ones()

    text = run(prompt_section.handle_tasks(command(ME), ""))

    assert old in text
    assert "我发起的（1）" in text
    assert "已完成（5）" in text  # DONE_LIMIT still caps the finished group
    assert text != "当前会话没有任务。用 task_add 把活派给别的会话，或等别人派给你。"


def test_tasks_groups_are_still_truncated_after_filtering(workspace):
    """The fix must not turn a group into "everything"."""

    for index in range(30):
        make_task(
            title=f"待办 {index}",
            from_session=OTHER,
            to_session=ME,
            at=f"2026-11-22T10:{index:02d}:00+08:00",
        )

    text = run(prompt_section.handle_tasks(command(ME), ""))

    assert f"待我处理（{prompt_section.GROUP_LIMIT}）" in text
    shown = [
        line
        for line in text.splitlines()
        if line.startswith("  • ") and "[submitted]" in line
    ]
    assert len(shown) == prompt_section.GROUP_LIMIT


def test_active_entries_ignores_a_blank_session(workspace):
    make_task()

    assert prompt_section._active_entries("   ") == []


# --------------------------------------------------------------------------- #
# provider()
# --------------------------------------------------------------------------- #
def test_provider_uses_the_session_on_the_context_object(workspace):
    task_id = make_task()

    text = prompt_section.provider({"session_id": ME})

    assert task_id in text


def test_provider_returns_empty_text_when_the_store_explodes(workspace, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(prompt_section, "_active_entries", boom)

    assert prompt_section.provider({"session_id": ME}) == ""


# --------------------------------------------------------------------------- #
# /tasks
# --------------------------------------------------------------------------- #
def test_tasks_prints_the_three_groups(workspace):
    incoming = make_task(title="等我审的合同", from_session=OTHER, to_session=ME)
    outgoing = make_task(title="交给别人的活")
    finished = make_task(title="已经做完的", states=("working", "completed"))
    foreign = make_task(from_session=FOREIGN, to_session="console:elsewhere")

    text = run(prompt_section.handle_tasks(command(ME), ""))

    assert "待我处理（1）" in text
    assert "我发起的（1）" in text
    assert "已完成（1）" in text
    assert incoming in text
    assert outgoing in text
    assert finished in text
    assert "[completed]" in text
    assert foreign not in text


def test_tasks_shows_at_most_five_finished_tasks(workspace):
    ids = [
        make_task(title=f"完成 {index}", states=("working", "completed"))
        for index in range(7)
    ]

    text = run(prompt_section.handle_tasks(command(ME), ""))
    finished_block = text.split("已完成（", 1)[1]

    assert sum(1 for task_id in ids if task_id in finished_block) == (
        prompt_section.DONE_LIMIT
    )


def test_tasks_without_any_task_returns_one_hint_line(workspace):
    text = run(prompt_section.handle_tasks(command(ME), ""))

    assert text == "当前会话没有任务。用 task_add 把活派给别的会话，或等别人派给你。"


def test_tasks_reads_raw_args_from_the_host_parser_shape(workspace):
    task_id = make_task()

    text = run(
        prompt_section.handle_tasks(
            {"session_id": ME, "args": {"_raw_args": "ignored"}}
        )
    )

    assert task_id in text


# --------------------------------------------------------------------------- #
# /task <id>
# --------------------------------------------------------------------------- #
def test_task_command_shows_the_view_of_one_task(workspace):
    task_id = make_task(title="写一份周报", states=("working",))
    views.append_event_once(task_id, {"type": "summary", "value": "三段已经写完"})

    text = run(prompt_section.handle_task(command(ME), task_id))

    assert task_id in text
    assert "写一份周报" in text
    assert "working" in text
    assert "三段已经写完" in text
    assert "发起：console:default" in text


def test_task_command_accepts_the_host_args_dict(workspace):
    task_id = make_task()

    text = run(
        prompt_section.handle_task(
            {"session_id": ME, "args": {"_raw_args": f"{task_id} extra words"}}
        )
    )

    assert task_id in text


def test_task_command_without_an_id_prints_usage(workspace):
    assert run(prompt_section.handle_task(command(ME), "")) == (
        "用法：/task <id>（id 可以先 /tasks 查）"
    )


def test_task_command_reports_an_unknown_id(workspace):
    text = run(prompt_section.handle_task(command(ME), "t-20260101-0000-deadbeef"))

    assert text.startswith("任务不存在：")


def test_task_command_refuses_a_stranger(workspace):
    task_id = make_task()

    text = run(prompt_section.handle_task(command(FOREIGN), task_id))

    assert "无权访问该任务" in text
    assert "写一份周报" not in text


# --------------------------------------------------------------------------- #
# /nudge <id> [note]
# --------------------------------------------------------------------------- #
def test_nudge_from_the_initiator_writes_an_event_and_keeps_the_state(workspace):
    task_id = make_task()
    before = views.materialize(task_id)

    text = run(prompt_section.handle_nudge(command(ME), f"{task_id} 还在等结果"))

    after = views.materialize(task_id)
    events = nudge_events(task_id)

    assert "已催办" in text
    assert len(events) == 1
    assert events[0]["note"] == "还在等结果"
    assert after["state"] == before["state"] == models.TaskState.SUBMITTED


def test_nudge_from_the_receiver_is_refused(workspace):
    task_id = make_task()

    text = run(prompt_section.handle_nudge(command(OTHER), f"{task_id} 快一点"))

    assert "只有任务发起方可以催办" in text
    assert nudge_events(task_id) == []
    assert views.materialize(task_id)["state"] == models.TaskState.SUBMITTED


def test_nudge_from_a_stranger_is_refused(workspace):
    task_id = make_task()

    text = run(prompt_section.handle_nudge(command(FOREIGN), task_id))

    assert "只有任务发起方可以催办" in text
    assert nudge_events(task_id) == []


def test_nudge_without_an_id_prints_usage(workspace):
    assert run(prompt_section.handle_nudge(command(ME), "   ")) == (
        "用法：/nudge <id> [备注]"
    )


def test_nudge_reports_an_unknown_id(workspace):
    text = run(prompt_section.handle_nudge(command(ME), "t-20260101-0000-deadbeef"))

    assert text.startswith("任务不存在：")


def test_nudge_without_a_note_still_records_the_reminder(workspace):
    task_id = make_task()

    text = run(prompt_section.handle_nudge(command(ME), task_id))

    events = nudge_events(task_id)
    assert len(events) == 1
    assert events[0]["value"] == ""
    assert "已催办" in text


# --------------------------------------------------------------------------- #
# Host adapters
# --------------------------------------------------------------------------- #
def test_adapters_expose_the_host_command_contract_and_return_text(workspace):
    task_id = make_task()

    handler = prompt_section.TasksCommandHandler()
    text = run(
        handler.handle(
            type(
                "Ctx",
                (),
                {"session_id": ME, "args": {"_raw_args": ""}},
            )()
        )
    )

    # Slash-free on the attribute (the host prefixes it for the channel
    # registry); slashed in the value the dispatcher keys on. See
    # ``prompt_section.CommandName`` and ``tests/test_host_bridge.py``.
    assert handler.command_name == "tasks"
    assert f"/{handler.command_name}" == "/tasks"
    assert handler.command_name.lower() == "/tasks"
    assert handler.description
    assert isinstance(text, str)
    assert task_id in text


# --------------------------------------------------------------------------- #
# plugin.register with a FakeApi
# --------------------------------------------------------------------------- #
class FakeApi:
    """Records every registration; rejects the anchor the real host rejects."""

    HOST_ANCHORS = ("workspace", "multimodal", "env_context")

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.prompt_sections: list[dict] = []
        self.commands: list = []
        self.routers: list = []
        self.skills: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_skill_provider(
        self, skills_dir, *, enabled_by_default=False, channels=None
    ) -> None:
        self.skills.append(
            {
                "skills_dir": skills_dir,
                "enabled_by_default": enabled_by_default,
                "channels": channels,
            }
        )

    def register_prompt_section(
        self, name, provider, *, after="workspace", agent_id=None
    ) -> None:
        if after not in self.HOST_ANCHORS:
            raise ValueError(
                f"prompt section after='{after}' must reference a host anchor"
            )
        self.prompt_sections.append(
            {"name": name, "provider": provider, "after": after, "agent_id": agent_id}
        )

    def register_control_command(self, handler, priority_level=10) -> None:
        self.commands.append(handler)

    def register_http_router(self, router, *, prefix, tags=None) -> None:
        self.routers.append((router, prefix))


def test_register_wires_tools_prompt_section_commands_and_router(workspace):
    api = FakeApi()

    plugin_module.plugin.register(api)

    assert [spec["tool_name"] for spec in api.tools] == [
        name for name, _, _, _ in plugin_module.TOOL_SPECS
    ]

    assert len(api.prompt_sections) == 1
    section = api.prompt_sections[0]
    assert section["name"] == prompt_section.SECTION_NAME
    assert section["after"] == prompt_section.SECTION_FALLBACK_ANCHOR
    assert section["agent_id"] is None
    assert callable(section["provider"])
    assert isinstance(section["provider"]({"session_id": ME}), str)

    # Slash-free attribute, slashed channel-registry key (host bridge).
    assert [handler.command_name for handler in api.commands] == [
        "tasks",
        "task",
        "nudge",
    ]
    assert [f"/{handler.command_name}" for handler in api.commands] == [
        "/tasks",
        "/task",
        "/nudge",
    ]
    assert all(handler.description for handler in api.commands)

    assert len(api.routers) == 1
    router, prefix = api.routers[0]
    assert prefix == "/session-task"
    assert {route.path for route in router.routes} == {"/tasks", "/tasks/{task_id}"}


def test_register_keeps_the_requested_anchor_when_the_host_accepts_it(workspace):
    api = FakeApi()
    api.HOST_ANCHORS = api.HOST_ANCHORS + (prompt_section.SECTION_ANCHOR,)

    plugin_module.plugin.register(api)

    assert api.prompt_sections[0]["after"] == prompt_section.SECTION_ANCHOR


def test_register_survives_a_host_with_only_register_tool(workspace):
    class MinimalApi:
        def __init__(self) -> None:
            self.tools: list[dict] = []

        def register_tool(self, **kwargs) -> None:
            self.tools.append(kwargs)

    api = MinimalApi()

    plugin_module.plugin.register(api)  # must not raise

    assert len(api.tools) == len(plugin_module.TOOL_SPECS)


def test_register_survives_a_broken_prompt_registration(workspace):
    class ExplodingApi(FakeApi):
        def register_prompt_section(self, *args, **kwargs) -> None:
            raise RuntimeError("host bug")

    api = ExplodingApi()

    plugin_module.plugin.register(api)

    assert api.prompt_sections == []
    # Slash-free attribute, slashed channel-registry key (host bridge).
    assert [handler.command_name for handler in api.commands] == [
        "tasks",
        "task",
        "nudge",
    ]
    assert [f"/{handler.command_name}" for handler in api.commands] == [
        "/tasks",
        "/task",
        "/nudge",
    ]


# --------------------------------------------------------------------------- #
# bundled skill provider (US-007)
# --------------------------------------------------------------------------- #
def test_register_ships_the_bundled_skill_enabled_by_default(workspace):
    api = FakeApi()

    plugin_module.plugin.register(api)

    assert len(api.skills) == 1
    entry = api.skills[0]
    assert entry["skills_dir"] == plugin_module.PLUGIN_DIR / "skills"
    assert entry["enabled_by_default"] is True
    assert entry["channels"] is None


def test_the_bundled_skill_has_a_name_and_a_description():
    skill = plugin_module.PLUGIN_DIR / "skills" / "session-task-playbook" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    front_matter = text.split("---\n", 2)[1]
    assert "name: session-task-playbook" in front_matter
    assert "description:" in front_matter
    body = text.split("---\n", 2)[2]
    assert "task_report" in body and "task_add" in body


def test_register_survives_a_broken_skill_provider(workspace):
    class ExplodingApi(FakeApi):
        def register_skill_provider(self, *args, **kwargs) -> None:
            raise RuntimeError("host bug")

    api = ExplodingApi()

    plugin_module.plugin.register(api)  # must not raise

    assert len(api.tools) == len(plugin_module.TOOL_SPECS)


def test_register_survives_a_host_without_a_skill_provider(workspace):
    class NoSkillApi(FakeApi):
        register_skill_provider = None

    api = NoSkillApi()

    plugin_module.plugin.register(api)

    assert api.skills == []
    # Slash-free attribute, slashed channel-registry key (host bridge).
    assert [handler.command_name for handler in api.commands] == [
        "tasks",
        "task",
        "nudge",
    ]
    assert [f"/{handler.command_name}" for handler in api.commands] == [
        "/tasks",
        "/task",
        "/nudge",
    ]



# --------------------------------------------------------------------------- #
# Session aliases (console:<id> vs <id>) — F1
# --------------------------------------------------------------------------- #
BARE_INITIATOR = "sub-alias-initiator"
QUALIFIED_INITIATOR = "console:" + BARE_INITIATOR
BARE_WORKER = "sub-alias-worker"


def test_tasks_finds_an_active_task_through_a_console_qualified_session(workspace):
    """``/tasks`` must not drop the task when the context is channel-qualified."""

    task_id = make_task(from_session=BARE_INITIATOR, to_session=BARE_WORKER)
    output = run(prompt_section.handle_tasks(command(QUALIFIED_INITIATOR)))

    assert task_id in output
    assert "我发起的（1）" in output
    assert "待我处理（0）" in output


def test_tasks_accepts_a_console_prefixed_recipient(workspace):
    """The event log holds the bare id, the context arrives qualified."""

    task_id = make_task(from_session=BARE_WORKER, to_session=QUALIFIED_INITIATOR)
    output = run(prompt_section.handle_tasks(command(BARE_INITIATOR)))

    assert task_id in output
    assert "待我处理（1）" in output


def test_task_command_lets_the_initiator_in_through_a_console_prefix(workspace):
    task_id = make_task(from_session=BARE_INITIATOR, to_session=BARE_WORKER)
    output = run(prompt_section.handle_task(command(QUALIFIED_INITIATOR, task_id)))

    assert output.startswith("任务 " + task_id)
    assert "无权访问" not in output


def test_task_command_still_refuses_a_stranger_through_a_console_prefix(workspace):
    task_id = make_task(from_session=BARE_INITIATOR, to_session=BARE_WORKER)
    output = run(prompt_section.handle_task(command("console:stranger", task_id)))

    assert "无权访问" in output


def test_nudge_lets_the_initiator_in_through_a_console_prefix(workspace):
    task_id = make_task(from_session=BARE_INITIATOR, to_session=BARE_WORKER)
    output = run(prompt_section.handle_nudge(command(QUALIFIED_INITIATOR, task_id)))

    assert "已催办" in output
    assert len(nudge_events(task_id)) == 1


def test_nudge_still_refuses_a_stranger_through_a_console_prefix(workspace):
    task_id = make_task(from_session=BARE_INITIATOR, to_session=BARE_WORKER)
    output = run(prompt_section.handle_nudge(command("console:stranger", task_id)))

    assert "只有任务发起方可以催办" in output
    assert nudge_events(task_id) == []
