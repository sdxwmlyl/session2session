"""Tests for the injected prompt texts (US-003).

Two contracts are pinned here:

* the worker card fits :data:`prompt.MAX_CARD_CHARS` and always contains the
  five tool calls plus the "you must report" sentence — including for
  pathological titles/goals — and never quotes the plan itself;
* the initiator summary is one line, stays under
  :data:`prompt.MAX_SUMMARY_CHARS`, and uses the right shape for each state.

The tests build views by hand *and* through ``views.materialize`` so a rename
in the view layer breaks here rather than in production.
"""

from __future__ import annotations

import pytest

import prompt
import store
import views
from prompt import (
    MAX_CARD_CHARS,
    MAX_SUMMARY_CHARS,
    initiator_summary,
    worker_task_card,
)

#: Words that would mean the card is leaking the plan into the worker prompt.
FORBIDDEN = ("PRD", "prd", "用户故事", "故事", "验收标准", "全部条目", "需求文档")


@pytest.fixture()
def isolated_workspace(tmp_path, monkeypatch):
    """Point the store at a temp workspace (for the materialize integration)."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    return tmp_path


def _view(**overrides):
    view = {
        "task_id": "t-20261122-2036-5779",
        "state": "working",
        "title": "整理 11 月客户回款明细",
        "goal": "导出 11 月回款流水并按客户汇总，异常单标注原因",
        "from_session": "console:default",
        "to_session": "console:worker",
        "summary": "已导出 3 张表，正在核对差异",
        "artifacts": [],
        "history": [],
        "updated_at": "2026-11-22T20:36:37+08:00",
    }
    view.update(overrides)
    return view


# --------------------------------------------------------------------------- #
# worker_task_card
# --------------------------------------------------------------------------- #
def test_card_fits_the_budget_for_a_normal_view():
    card = worker_task_card(_view())

    assert len(card) <= MAX_CARD_CHARS
    assert card.strip() == card


def test_card_names_the_task_the_initiator_and_the_goal():
    card = worker_task_card(_view())

    assert "t-20261122-2036-5779" in card
    assert "整理 11 月客户回款明细" in card
    assert "console:default" in card
    assert "按客户汇总" in card


def test_card_lists_the_five_reporting_steps():
    card = worker_task_card(_view())

    assert "task_get" in card
    assert "task_pulse" in card
    assert "'收到'" in card
    assert "'working'" in card
    assert "'completed'" in card
    assert "'input-required'" in card
    assert "task_report" in card


def test_card_ends_with_the_must_report_sentence():
    card = worker_task_card(_view())

    assert card.splitlines()[-1] == prompt.CARD_CLOSING
    assert "report" in card.splitlines()[-1]
    assert "静默" in card.splitlines()[-1]


def test_card_does_not_quote_the_plan():
    # A card is a pointer to one task, not a copy of the plan it came from.
    card = worker_task_card(_view())

    lowered = card.lower()
    for word in FORBIDDEN:
        assert word.lower() not in lowered


@pytest.mark.parametrize(
    "view",
    [
        _view(title="题" * 200, goal="标" * 4000, from_session="x" * 80),
        _view(task_id="t-" * 16, title="题" * 200, goal="标" * 4000),
        _view(task_id="", title="", goal="", from_session="", summary=""),
        _view(title="a\nb\r\nc\td", goal="line1\nline2"),
        {"task_id": "t-1"},
        {},
    ],
    ids=[
        "long-fields",
        "long-id",
        "empty-fields",
        "multiline",
        "minimal",
        "empty-view",
    ],
)
def test_card_stays_within_the_budget_for_hostile_input(view):
    card = worker_task_card(view)

    assert len(card) <= MAX_CARD_CHARS
    assert prompt.CARD_CLOSING in card
    assert "None" not in card


def test_card_keeps_a_multi_line_title_on_one_line():
    card = worker_task_card(_view(title="第一行\n第二行"))

    assert "第一行 第二行" in card
    assert len(card.splitlines()) == len(prompt.CARD_STEP_LINES) + 3


def test_card_degrades_missing_fields_to_placeholders():
    card = worker_task_card({"task_id": "t-1"})

    assert "t-1" in card
    assert "未命名任务" in card
    assert "未知会话" in card


def test_card_reads_a_materialised_view(isolated_workspace):
    task_id = store.new_task_id()
    store.append_event(
        task_id,
        {
            "type": "created",
            "title": "写周报",
            "goal": "把本周进展整理成三段",
            "from_session": "console:default",
            "to_session": "console:worker",
        },
    )
    store.append_event(task_id, {"type": "state", "value": "working"})

    card = worker_task_card(views.materialize(task_id))

    assert task_id in card
    assert "写周报" in card
    assert "把本周进展整理成三段" in card


# --------------------------------------------------------------------------- #
# initiator_summary: shapes
# --------------------------------------------------------------------------- #
def test_summary_for_completed_states_the_result():
    line = initiator_summary(_view(state="completed", summary="已完成汇总"))

    assert line.startswith("✅ ")
    assert "完成：" in line
    assert "已完成汇总" in line


def test_summary_for_completed_counts_artifacts():
    line = initiator_summary(
        _view(state="completed", summary="ok", artifacts=["a.json", "b.xlsx"])
    )

    assert "（2 个产物）" in line


def test_summary_for_completed_without_artifacts_has_no_count():
    line = initiator_summary(_view(state="completed", summary="ok", artifacts=[]))

    assert "个产物" not in line


def test_summary_for_input_required_asks_the_initiator():
    line = initiator_summary(_view(state="input-required", summary="缺少对账单"))

    assert line.startswith("⏸ ")
    assert "需要你：" in line
    assert "缺少对账单" in line


def test_summary_for_failed_reports_the_failure():
    line = initiator_summary(_view(state="failed", summary="接口超时"))

    assert line.startswith("❌ ")
    assert "失败：" in line
    assert "接口超时" in line


def test_summary_for_canceled_ignores_the_worker_text():
    line = initiator_summary(_view(state="canceled", summary="不需要了"))

    assert line.startswith("🚫 ")
    assert line.endswith("已取消")
    assert "不需要了" not in line


@pytest.mark.parametrize("state", ["working", "submitted", "something-new", None])
def test_summary_for_any_other_state_is_in_progress(state):
    line = initiator_summary(_view(state=state, summary="半成品"))

    assert line.startswith("▶ ")
    assert line.endswith("进行中")
    assert "半成品" not in line


# --------------------------------------------------------------------------- #
# initiator_summary: limits and degradations
# --------------------------------------------------------------------------- #
def test_summary_keeps_only_the_first_characters_of_the_worker_text():
    line = initiator_summary(_view(state="completed", title="短", summary="甲" * 200))

    # 80 characters of body including the ellipsis that marks the cut.
    assert "甲" * 79 + "…" in line
    assert "甲" * 81 not in line


def test_summary_shortens_the_result_when_the_title_is_long():
    line = initiator_summary(
        _view(state="completed", title="题" * 200, summary="甲" * 200)
    )

    assert "甲" * 80 not in line
    assert len(line) <= MAX_SUMMARY_CHARS


def test_summary_without_a_summary_keeps_the_verb_bare():
    line = initiator_summary(_view(state="completed", summary=None))

    assert line.startswith("✅ ")
    assert line.endswith("完成")
    assert "：" not in line
    assert "None" not in line


def test_summary_without_a_title_uses_a_placeholder():
    line = initiator_summary(_view(state="failed", title=None, summary="炸了"))

    assert "未命名任务" in line


@pytest.mark.parametrize(
    "view",
    [
        _view(state="completed", title="题" * 200, summary="结" * 4000),
        _view(
            state="completed",
            title="题" * 200,
            summary="结" * 4000,
            artifacts=["a"] * 30,
        ),
        _view(
            state="completed", title="t" * 200, summary="s" * 4000, artifacts=["a"] * 99
        ),
        _view(state="failed", title="题" * 200, summary="结" * 4000),
        _view(state="completed", summary="第一行\n第二行"),
    ],
    ids=["long", "many-artifacts", "ninety-nine-artifacts", "failed-long", "multiline"],
)
def test_summary_is_one_short_line_for_hostile_input(view):
    line = initiator_summary(view)

    assert len(line) <= MAX_SUMMARY_CHARS
    assert "\n" not in line
    assert line.strip() == line


def test_summary_reads_a_materialised_view(isolated_workspace):
    task_id = store.new_task_id()
    store.append_event(
        task_id,
        {
            "type": "created",
            "title": "写周报",
            "goal": "三段",
            "from_session": "console:default",
            "to_session": "console:worker",
        },
    )
    store.append_event(task_id, {"type": "state", "value": "working"})
    store.append_event(task_id, {"type": "summary", "value": "已发群"})
    store.append_event(task_id, {"type": "state", "value": "completed"})
    store.append_event(task_id, {"type": "artifacts", "value": ["周报.docx"]})
    view = views.materialize(task_id)

    line = initiator_summary(view)

    assert line.startswith("✅ ")
    assert "写周报" in line
    assert "已发群" in line
    assert "（1 个产物）" in line
