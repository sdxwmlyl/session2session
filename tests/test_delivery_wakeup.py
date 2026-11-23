# -*- coding: utf-8 -*-
"""``delivery`` wake-up path: port discovery + console chat transport."""

import json
import os

import pytest

import delivery


class _Response:
    status = 200

    def __init__(self, body=b"data: {}\n\n"):
        self._body = body

    def read(self, n=-1):
        return self._body if n in (-1, None) else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in delivery.DRY_RUN_ENV_ALIASES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(delivery.API_PORT_ENV, raising=False)
    monkeypatch.delenv(delivery.API_HOST_ENV, raising=False)


def test_api_base_reads_desktop_port_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".copaw").mkdir(parents=True)
    (home / ".copaw" / "desktop_port").write_text("64445\n", encoding="utf-8")
    monkeypatch.setenv("QWENPAW_HOME", str(home))
    assert delivery.api_base() == "http://127.0.0.1:64445"


def test_env_wins_over_desktop_port(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".copaw").mkdir(parents=True)
    (home / ".copaw" / "desktop_port").write_text("64445", encoding="utf-8")
    monkeypatch.setenv("QWENPAW_HOME", str(home))
    monkeypatch.setenv("QWENPAW_API_PORT", "6001")
    assert delivery.api_base() == "http://127.0.0.1:6001"


def test_console_chat_transport_body_and_path(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["agent"] = request.get_header("X-agent-id")
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(delivery, "urlopen", fake_urlopen)
    send = delivery.console_chat_transport("http://127.0.0.1:64445", agent="default")
    send("adfba159-199b-41d8-8541-2cdd8ba057fe", "任务卡正文")

    assert seen["url"].endswith("/api/console/chat")
    assert seen["agent"] == "default"
    assert seen["timeout"] == delivery.HTTP_TIMEOUT_SECONDS
    body = seen["body"]
    assert body["channel"] == "console"
    assert body["session_id"] == "adfba159-199b-41d8-8541-2cdd8ba057fe"
    assert body["input"][0]["content"][0] == {"type": "text", "text": "任务卡正文"}


def test_routed_transport_sends_channel_ids_to_messages(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _Response(b'{"success": true}')

    monkeypatch.setattr(delivery, "urlopen", fake_urlopen)
    transport = delivery.routed_transport("http://127.0.0.1:64445")
    transport("wechat:wxid_abc", "摘要")
    transport("adfba159-199b-41d8-8541-2cdd8ba057fe", "任务卡")

    assert calls[0].endswith("/api/messages/send")
    assert calls[1].endswith("/api/console/chat")


def test_routed_transport_none_without_base(monkeypatch, tmp_path):
    import store

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path / "ws"))
    monkeypatch.setenv("QWENPAW_HOME", str(tmp_path / "empty"))
    assert delivery.routed_transport() is None


def test_console_transport_reports_http_error(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout=None):
        raise HTTPError(request.full_url, 503, "busy", None, None)

    monkeypatch.setattr(delivery, "urlopen", fake_urlopen)
    send = delivery.console_chat_transport("http://127.0.0.1:64445")
    with pytest.raises(delivery.DeliveryError) as exc:
        send("sess-1", "x")
    assert "503" in str(exc.value)


def test_agent_id_ignores_a_non_agent_workspace(monkeypatch, tmp_path):
    import store

    copaw = tmp_path / ".copaw"
    copaw.mkdir()
    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(copaw))
    monkeypatch.delenv(delivery.AGENT_ID_ENV, raising=False)
    assert delivery.agent_id() == "default"


def test_deliver_async_keeps_the_event_loop_free():
    """A blocking transport must not freeze the host loop (self-deadlock)."""
    import asyncio
    import time

    ticks = []

    def slow_transport(session_id, text):
        time.sleep(0.2)

    async def run():
        async def ticker():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        result = await delivery.deliver_async("sess", "x", transport=slow_transport)
        await task
        return result

    result = asyncio.run(run())
    assert result == {"ok": True}
    assert len(ticks) >= 10  # the loop kept running during the blocking send


