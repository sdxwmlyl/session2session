"""Tests for the US-010 reliability layer (``reliability.py``).

The upstream 2.3.0 ``PluginApi`` has neither ``register_runtime_hook`` nor
``register_agent_stop_handler`` (see the module docstring for file:line
evidence), so every test drives the callbacks directly or through a
``FakeApi``. The point is to prove the *logic* is right, so that wiring it up
the day upstream exposes the two methods is a one-line change.
"""

from __future__ import annotations

import logging

import pytest

import reliability
import store
import views


@pytest.fixture()
def isolated_workspace(tmp_path, monkeypatch):
    """Point the store at a temp workspace, never at the real one."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def clean_marks():
    """Every test starts with no pulsed / watermark / block marks."""

    reliability.reset_state()
    yield
    reliability.reset_state()


ME = "console:worker"
OTHER = "console:initiator"


def _task(*, state: str = "working", session: str = ME) -> str:
    """Create one task through the real store and move it to ``state``.

    ``state="submitted"`` leaves it where ``task_add`` puts it; anything else
    goes submitted -> working -> ``state``, because ``submitted -> completed``
    is deliberately not an edge in the state machine.
    """

    task_id = store.new_task_id()
    views.append_event_once(
        task_id,
        {
            "type": "created",
            "title": "对齐二期可靠性层",
            "goal": "跑通 auto-pulse 与 stop guard",
            "from_session": OTHER,
            "to_session": session,
        },
    )
    if state == "submitted":
        return task_id
    views.append_state(task_id, "working")
    if state != "working":
        views.append_state(task_id, state)
    return task_id


def _events(task_id: str, event_type: str) -> list[dict]:
    return [e for e in store.read_events(task_id) if e.get("type") == event_type]


def _ctx(**extra):
    context = {"session_id": ME}
    context.update(extra)
    return context


# --------------------------------------------------------------------------- #
# install(): present / absent / partial
# --------------------------------------------------------------------------- #
class FakeApi:
    """Minimal stand-in for ``PluginApi`` with the two optional methods."""

    def __init__(self, *, runtime_hook: bool = True, stop_handler: bool = True):
        self.calls: list[tuple] = []
        if runtime_hook:
            self.register_runtime_hook = self._runtime_hook
        if stop_handler:
            self.register_agent_stop_handler = self._stop_handler

    def _runtime_hook(self, phase, callback):
        self.calls.append(("runtime_hook", phase, callback))

    def _stop_handler(self, handler):
        self.calls.append(("stop_handler", handler))


def test_install_registers_both_surfaces_when_the_host_has_them():
    api = FakeApi()

    result = reliability.install(api)

    assert result == {
        "installed": ["register_runtime_hook", "register_agent_stop_handler"],
        "missing": [],
    }
    assert [call[:2] for call in api.calls] == [
        ("runtime_hook", reliability.PHASE_FINALLY),
        ("stop_handler", reliability.on_agent_stop),
    ]
    assert api.calls[0][2] is reliability.on_turn_finally


def test_install_reports_both_missing_and_warns_on_a_230_host(caplog):
    api = FakeApi(runtime_hook=False, stop_handler=False)

    with caplog.at_level(logging.WARNING):
        result = reliability.install(api)

    assert result["installed"] == []
    assert result["missing"] == [
        "register_runtime_hook",
        "register_agent_stop_handler",
    ]
    assert "no register_runtime_hook" in caplog.text
    assert "no register_agent_stop_handler" in caplog.text


def test_install_registers_only_the_surface_that_exists():
    api = FakeApi(stop_handler=False)

    result = reliability.install(api)

    assert result == {
        "installed": ["register_runtime_hook"],
        "missing": ["register_agent_stop_handler"],
    }


def test_install_survives_a_host_that_raises_while_registering():
    class BrokenApi:
        def register_runtime_hook(self, phase, callback):
            raise RuntimeError("boom")

        def register_agent_stop_handler(self, handler):
            raise RuntimeError("boom")

    result = reliability.install(BrokenApi())

    assert result == {"installed": [], "missing": []}


# --------------------------------------------------------------------------- #
# FINALLY hook → auto pulse
# --------------------------------------------------------------------------- #
def test_finally_hook_pulses_a_silent_working_task(isolated_workspace):
    task_id = _task()
    text = "正在核对第 3 批回款" * 20  # 160 chars

    result = reliability.on_turn_finally(**_ctx(assistant_text=text))

    assert result["pulsed"] == [task_id]
    pulses = _events(task_id, "pulse")
    assert len(pulses) == 1
    assert pulses[0]["value"] == text[: reliability.NOTE_CHARS]
    assert len(pulses[0]["value"]) <= 80
    assert pulses[0]["auto"] is True


def test_finally_hook_writes_an_empty_note_when_the_host_sends_no_text(
    isolated_workspace,
):
    task_id = _task()

    result = reliability.on_turn_finally(**_ctx())

    assert result["pulsed"] == [task_id]
    assert _events(task_id, "pulse")[0]["value"] == ""


def test_finally_hook_does_nothing_without_a_working_task(isolated_workspace):
    finished = _task(state="completed")

    result = reliability.on_turn_finally(**_ctx(assistant_text="收尾"))

    assert result == {"session": ME, "pulsed": [], "skipped": []}
    assert _events(finished, "pulse") == []


def test_finally_hook_pulses_only_once_per_turn(isolated_workspace):
    task_id = _task()

    first = reliability.on_turn_finally(**_ctx(turn_id="turn-1", assistant_text="第一步"))
    second = reliability.on_turn_finally(
        **_ctx(turn_id="turn-1", assistant_text="第一步")
    )

    assert first["pulsed"] == [task_id]
    assert second["pulsed"] == []
    assert second["skipped"] == [task_id]
    assert len(_events(task_id, "pulse")) == 1


def test_finally_hook_does_not_pulse_a_task_that_already_reported(
    isolated_workspace,
):
    task_id = _task()
    views.append_event_once(
        task_id, {"type": "summary", "value": "已完成 60%", "artifacts": []}
    )

    result = reliability.on_turn_finally(**_ctx(turn_id="turn-1"))

    assert result["pulsed"] == []
    assert result["skipped"] == [task_id]
    assert _events(task_id, "pulse") == []


def test_a_later_turn_pulses_again_after_an_earlier_auto_pulse(
    isolated_workspace,
):
    task_id = _task()

    reliability.on_turn_finally(**_ctx(turn_id="turn-1"))
    result = reliability.on_turn_finally(**_ctx(turn_id="turn-2"))

    assert result["pulsed"] == [task_id]
    assert len(_events(task_id, "pulse")) == 2


# --------------------------------------------------------------------------- #
# stop guard
# --------------------------------------------------------------------------- #
def test_stop_guard_blocks_twice_then_lets_the_agent_go(isolated_workspace):
    task_id = _task()
    context = _ctx(turn_id="turn-1")

    first = reliability.on_agent_stop(**context)
    second = reliability.on_agent_stop(**context)
    third = reliability.on_agent_stop(**context)

    assert (first["decision"], first["attempt"]) == ("BLOCK", 1)
    assert (second["decision"], second["attempt"]) == ("BLOCK", 2)
    assert first["blocked"] is True
    assert task_id in first["continuation"]
    assert "task_report" in first["continuation"]
    assert "task_pulse" in first["continuation"]
    assert third["decision"] == "ALLOW"
    assert third["blocked"] is False
    assert third["continuation"] is None


def test_stop_guard_allows_a_task_that_reported_this_turn(isolated_workspace):
    task_id = _task()
    views.append_event_once(task_id, {"type": "summary", "value": "70%", "artifacts": []})

    result = reliability.on_agent_stop(**_ctx(turn_id="turn-1"))

    assert result["decision"] == "ALLOW"
    assert result["blocked"] is False


def test_stop_guard_allows_a_task_that_pulsed_this_turn(isolated_workspace):
    task_id = _task()
    views.append_event_once(task_id, {"type": "pulse", "value": "还在跑"})

    result = reliability.on_agent_stop(**_ctx(turn_id="turn-1"))

    assert result["decision"] == "ALLOW"


def test_stop_guard_allows_after_the_finally_hook_auto_pulsed(
    isolated_workspace,
):
    task_id = _task()

    reliability.on_turn_finally(**_ctx(turn_id="turn-1"))
    result = reliability.on_agent_stop(**_ctx(turn_id="turn-1"))

    assert result["decision"] == "ALLOW"
    assert _events(task_id, "pulse") != []


def test_stop_guard_allows_a_turn_with_no_working_task(isolated_workspace):
    finished = _task(state="completed")

    result = reliability.on_agent_stop(**_ctx(turn_id="turn-1"))

    assert result["decision"] == "ALLOW"
    assert result["task_id"] is None


def test_stop_guard_counts_each_task_separately(isolated_workspace):
    first_task = _task()
    second_task = _task()
    context = _ctx(turn_id="turn-1")

    # The guard always names the first silent task it finds; two calls spend
    # that task's two blocks.
    attempts = [reliability.on_agent_stop(**context) for _ in range(2)]
    assert [d["decision"] for d in attempts] == ["BLOCK", "BLOCK"]
    assert {d["task_id"] for d in attempts} == {attempts[0]["task_id"]}
    blocked_task = attempts[0]["task_id"]
    other_task = second_task if blocked_task == first_task else first_task
    assert reliability._BLOCKS[blocked_task] == 2

    # Once that task reports, the *other* task starts from its own zero.
    views.append_event_once(blocked_task, {"type": "pulse", "value": "打点"})
    decision = reliability.on_agent_stop(**context)

    assert decision["decision"] == "BLOCK"
    assert decision["task_id"] == other_task
    assert decision["attempt"] == 1
    assert reliability._BLOCKS[other_task] == 1
    assert reliability._BLOCKS[blocked_task] == 2


def test_reset_state_clears_pulsed_and_block_marks(isolated_workspace):
    task_id = _task()
    reliability.on_turn_finally(**_ctx(turn_id="turn-1"))
    reliability.on_agent_stop(**_ctx(turn_id="turn-1"))

    reliability.reset_state()

    assert reliability._PULSED == set()
    assert reliability._BLOCKS == {}
    assert reliability._WATERMARK == {}

    # The in-process marks are gone, but the pulse written before the reset is
    # still on disk — activity, so that task is skipped and a fresh task is
    # the one that gets pulsed. That is what makes a restarted worker safe.
    fresh = _task()
    result = reliability.on_turn_finally(**_ctx(turn_id="turn-1"))

    assert result["pulsed"] == [fresh]
    assert result["skipped"] == [task_id]
    assert len(_events(task_id, "pulse")) == 1
