"""Delivery wiring (US-009).

``task_add`` pushes the worker card to the receiving session, and
``task_report`` / ``task_cancel`` push the one-line summary back to the
initiator. Every send here is *best effort*: the event log is the source of
truth, so a dead channel must not fail the tool call.

The dry-run outbox is asserted through its file, not through the result dict:
the dict is part of the delivery contract already covered by
``test_delivery.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import delivery
import store
import tools


@pytest.fixture(autouse=True)
def plain_payloads(monkeypatch):
    monkeypatch.setattr(tools, "ToolResponse", None)
    monkeypatch.setattr(tools, "TextBlock", None)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    for name in (delivery.API_PORT_ENV, delivery.API_HOST_ENV, *delivery.DRY_RUN_ENV_ALIASES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(delivery, "_INPROC_TRANSPORT", None)
    return tmp_path


@pytest.fixture()
def sent():
    """An installed transport that records what the tools send."""

    recorded: list = []

    def _transport(session_id, text):
        recorded.append((session_id, text))

    delivery.set_transport(_transport)
    yield recorded
    delivery.set_transport(None)


def _add(to_session="sess-b"):
    return asyncio.run(
        tools.task_add(to_session=to_session, title="写周报", goal="一页周报，含数据")
    )


def _report(task_id, state, summary):
    return asyncio.run(
        tools.task_report(task_id=task_id, state=state, summary=summary)
    )


# --------------------------------------------------------------------------- #
# task_add → worker card
# --------------------------------------------------------------------------- #
def test_task_add_pushes_the_card_to_the_receiving_session(
    workspace, sent, monkeypatch
):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")

    task_id = _add()

    assert isinstance(task_id, str) and task_id.startswith("t-")
    assert len(sent) == 1
    target, text = sent[0]
    assert target == "sess-b"
    assert f"任务 id={task_id}" in text
    assert "report" in text


def test_a_broken_channel_does_not_lose_the_task(workspace, monkeypatch):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")

    def _explode(session_id, text):
        raise RuntimeError("socket went away")

    delivery.set_transport(_explode)
    try:
        task_id = _add()
    finally:
        delivery.set_transport(None)

    assert isinstance(task_id, str) and task_id.startswith("t-")
    assert [event["type"] for event in store.read_events(task_id)] == ["created"]


def test_dry_run_records_the_card_in_the_outbox(workspace, monkeypatch):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")
    monkeypatch.setenv("QWENPAW_TASK_DRY_RUN", "1")

    task_id = _add()

    lines = delivery.dry_run_log_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["target"] == "sess-b"
    assert f"任务 id={task_id}" in record["text"]
    assert record["chars"] == len(record["text"])


# --------------------------------------------------------------------------- #
# task_report → initiator summary
# --------------------------------------------------------------------------- #
def test_working_does_not_notify_the_initiator(workspace, sent, monkeypatch):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")
    task_id = _add()
    sent.clear()

    _report(task_id, tools.models.TaskState.WORKING, "刚开始")

    assert sent == []


def test_completed_pushes_the_summary_to_the_initiator(
    workspace, sent, monkeypatch
):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")
    task_id = _add()
    _report(task_id, tools.models.TaskState.WORKING, "刚开始")
    sent.clear()

    _report(task_id, tools.models.TaskState.COMPLETED, "周报已发")

    assert len(sent) == 1
    target, text = sent[0]
    assert target == "sess-a"
    assert text.startswith("✅ ")
    assert "周报已发" in text


def test_cancelling_pushes_a_cancel_line_to_the_initiator(
    workspace, sent, monkeypatch
):
    monkeypatch.setenv(tools.SESSION_ENV, "sess-a")
    task_id = _add()
    sent.clear()

    asyncio.run(tools.task_cancel(task_id=task_id, reason="不做了"))

    assert [target for target, _ in sent] == ["sess-a"]
    assert "已取消" in sent[0][1]
