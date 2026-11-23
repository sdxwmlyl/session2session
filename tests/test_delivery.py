"""Tests for the delivery layer (US-003).

Everything here is offline: the transport is a test double, ``urlopen`` is
monkeypatched, and no channel is ever contacted. What is pinned down:

* an injected transport receives exactly ``(session_id, text)``;
* a failing transport becomes ``{"ok": False, "error": ...}`` and is logged
  with its cause — never swallowed, never raised at the caller;
* ``split_session`` parses ``<channel>:<user>`` and refuses everything else;
* the host wiring (``api_base`` / ``agent_id`` / ``http_transport``) builds
  the same request the CLI sends: ``POST /api/messages/send`` with
  ``X-Agent-Id`` and ``{channel, target_user, target_session, text}``;
* "no transport available" is what a caller sees when the plugin is loaded
  outside a host and nothing was installed.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

import delivery
import store
from delivery import DeliveryError, deliver, deliver_async, split_session


class _FakeResponse:
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __init__(self, body: bytes = b'{"success": true}', status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """Temp workspace, no host context, no env overrides, no transport."""

    monkeypatch.setattr(store, "_host_workspace_dir", lambda: None)
    monkeypatch.setenv(store.WORKSPACE_ENV, str(tmp_path))
    # Keep the desktop launcher's port file (a real one on a live host) out of
    # the picture: this suite asserts "no base url" cases too.
    monkeypatch.setenv("QWENPAW_HOME", str(tmp_path / "home"))
    for name in (
        delivery.API_PORT_ENV,
        delivery.API_HOST_ENV,
        delivery.AGENT_ID_ENV,
        delivery.DRY_RUN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(delivery, "_INPROC_TRANSPORT", None)
    return tmp_path


@pytest.fixture()
def calls():
    """A transport that records its calls and succeeds."""

    recorded: list = []

    def _transport(session_id, text):
        recorded.append((session_id, text))

    _transport.recorded = recorded
    return _transport


def _write_config(workspace, **config):
    (workspace / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# split_session
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("console:default", ("console", "default")),
        ("wechat:wxid_abc123", ("wechat", "wxid_abc123")),
        ("  console:default  ", ("console", "default")),
        ("console:a:b", ("console", "a:b")),
        ("Console:Default", ("Console", "Default")),
    ],
)
def test_split_session_parses_a_channel_and_a_user(raw, expected):
    assert split_session(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, 42, b"console:default", "", "   ", "console", ":default", "console:"]
)
def test_split_session_refuses_what_it_cannot_route(raw):
    assert split_session(raw) is None


# --------------------------------------------------------------------------- #
# deliver: injected transport
# --------------------------------------------------------------------------- #
def test_deliver_passes_the_session_and_the_text_to_the_transport(clean_env, calls):
    result = deliver("console:default", "✅ 写完了 完成：ok", transport=calls)

    assert result == {"ok": True}
    assert calls.recorded == [("console:default", "✅ 写完了 完成：ok")]


def test_deliver_strips_the_session_id(clean_env, calls):
    deliver("  console:default  ", "hi", transport=calls)

    assert calls.recorded == [("console:default", "hi")]


def test_deliver_returns_the_error_when_the_transport_raises(clean_env, caplog):
    def _explode(session_id, text):
        raise RuntimeError("socket went away")

    with caplog.at_level(logging.WARNING, logger="qwenpaw"):
        result = deliver("console:default", "hi", transport=_explode)

    assert result == {"ok": False, "error": "RuntimeError: socket went away"}
    assert "socket went away" in caplog.text


def test_deliver_keeps_a_delivery_error_verbatim(clean_env):
    def _refuse(session_id, text):
        raise DeliveryError("no transport available")

    assert deliver("console:default", "hi", transport=_refuse) == {
        "ok": False,
        "error": "no transport available",
    }


@pytest.mark.parametrize(
    "session_id,text,needle",
    [
        ("", "hi", "session_id"),
        ("   ", "hi", "session_id"),
        (None, "hi", "session_id"),
        (123, "hi", "session_id"),
        ("console:default", None, "text"),
        ("console:default", "   ", "text"),
    ],
)
def test_deliver_validates_its_arguments(clean_env, calls, session_id, text, needle):
    result = deliver(session_id, text, transport=calls)

    assert result["ok"] is False
    assert needle in result["error"]
    assert calls.recorded == []


# --------------------------------------------------------------------------- #
# deliver: default transport resolution
# --------------------------------------------------------------------------- #
def test_default_transport_is_unavailable_outside_a_host(clean_env, monkeypatch):
    monkeypatch.setattr(delivery, "api_base", lambda: None)

    result = deliver("console:default", "hi")

    assert result == {"ok": False, "error": "no transport available"}


def test_an_installed_transport_wins_over_http(clean_env, calls, monkeypatch):
    monkeypatch.setattr(delivery, "api_base", lambda: "http://127.0.0.1:9")
    delivery.set_transport(calls)
    try:
        result = deliver("console:default", "hi")
    finally:
        delivery.set_transport(None)

    assert result == {"ok": True}
    assert calls.recorded == [("console:default", "hi")]
    assert delivery.get_transport() is None


def test_set_transport_rejects_non_callables(clean_env):
    with pytest.raises(TypeError):
        delivery.set_transport("nope")


def test_default_transport_falls_back_to_http(clean_env, monkeypatch):
    _write_config(clean_env, last_api={"host": "127.0.0.1", "port": 64447})

    transport = delivery.default_transport()

    assert transport is not None
    assert transport.__name__ == "_transport"


# --------------------------------------------------------------------------- #
# deliver: dry run
# --------------------------------------------------------------------------- #
def test_dry_run_skips_the_transport(clean_env, monkeypatch):
    def _never(session_id, text):  # pragma: no cover - must not be called
        raise AssertionError("dry run must not send")

    monkeypatch.setenv(delivery.DRY_RUN_ENV, "1")

    result = deliver("console:default", "hi", transport=_never)

    assert result == {
        "ok": True,
        "dry_run": True,
        "target": "console:default",
        "chars": 2,
    }


# --------------------------------------------------------------------------- #
# host wiring: api_base / agent_id
# --------------------------------------------------------------------------- #
def test_api_base_reads_last_api_from_the_workspace_config(clean_env):
    _write_config(clean_env, last_api={"host": "127.0.0.1", "port": 64447})

    assert delivery.api_base() == "http://127.0.0.1:64447"


def test_api_base_reads_a_top_level_port_too(clean_env):
    _write_config(clean_env, port=8088)

    assert delivery.api_base() == "http://127.0.0.1:8088"


def test_api_base_prefers_the_environment(clean_env, monkeypatch):
    _write_config(clean_env, last_api={"host": "127.0.0.1", "port": 64447})
    monkeypatch.setenv(delivery.API_PORT_ENV, "9001")
    monkeypatch.setenv(delivery.API_HOST_ENV, "192.168.1.5")

    assert delivery.api_base() == "http://192.168.1.5:9001"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"last_api": {}},
        {"last_api": {"port": "not-a-port"}},
        {"last_api": {"port": 70000}},
    ],
)
def test_api_base_is_none_without_a_usable_port(clean_env, config):
    _write_config(clean_env, **config)

    assert delivery.api_base() is None


def test_api_base_is_none_without_a_config_file(clean_env):
    assert delivery.api_base() is None


def test_api_base_is_none_for_a_corrupt_config_file(clean_env):
    (clean_env / "config.json").write_text("{not json", encoding="utf-8")

    assert delivery.api_base() is None


def test_agent_id_follows_the_workspace_name(clean_env):
    assert delivery.agent_id() == clean_env.name


def test_agent_id_can_be_overridden(clean_env, monkeypatch):
    monkeypatch.setenv(delivery.AGENT_ID_ENV, "worker-bot")

    assert delivery.agent_id() == "worker-bot"


# --------------------------------------------------------------------------- #
# http_transport
# --------------------------------------------------------------------------- #
def test_http_transport_posts_the_host_payload(clean_env, monkeypatch):
    _write_config(clean_env, last_api={"host": "127.0.0.1", "port": 64447})
    seen = {}

    def _fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["payload"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(delivery, "urlopen", _fake_urlopen)

    result = deliver("console:default", "任务完成", transport=delivery.http_transport())

    assert result == {"ok": True}
    assert seen["url"] == "http://127.0.0.1:64447/api/messages/send"
    assert seen["method"] == "POST"
    assert seen["headers"]["x-agent-id"] == clean_env.name
    assert seen["headers"]["content-type"].startswith("application/json")
    assert seen["payload"] == {
        "channel": "console",
        "target_user": "default",
        "target_session": "console:default",
        "text": "任务完成",
    }
    assert seen["timeout"] == delivery.HTTP_TIMEOUT_SECONDS


def test_http_transport_uses_an_explicit_base_url_and_agent(clean_env, monkeypatch):
    seen = {}

    def _fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["agent"] = {k.lower(): v for k, v in request.headers.items()}["x-agent-id"]
        return _FakeResponse()

    monkeypatch.setattr(delivery, "urlopen", _fake_urlopen)

    deliver(
        "wechat:wxid_abc",
        "hi",
        transport=delivery.http_transport("http://10.0.0.2:8088/", agent="bot-1"),
    )

    assert seen["url"] == "http://10.0.0.2:8088/api/messages/send"
    assert seen["agent"] == "bot-1"


def test_http_transport_reports_an_http_error(clean_env, monkeypatch):
    from urllib.error import HTTPError

    def _fake_urlopen(request, timeout=None):
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(delivery, "urlopen", _fake_urlopen)

    result = deliver(
        "console:default", "hi", transport=delivery.http_transport("http://127.0.0.1:1")
    )

    assert result["ok"] is False
    assert "HTTP 404" in result["error"]
    assert "/api/messages/send" in result["error"]


def test_http_transport_reports_an_unreachable_host(clean_env, monkeypatch):
    from urllib.error import URLError

    def _fake_urlopen(request, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(delivery, "urlopen", _fake_urlopen)

    result = deliver(
        "console:default", "hi", transport=delivery.http_transport("http://127.0.0.1:1")
    )

    assert result["ok"] is False
    assert "cannot reach" in result["error"]


def test_http_transport_reports_a_refused_message(clean_env, monkeypatch):
    monkeypatch.setattr(
        delivery,
        "urlopen",
        lambda request, timeout=None: _FakeResponse(
            b'{"success": false, "message": "channel not found"}'
        ),
    )

    result = deliver(
        "console:default", "hi", transport=delivery.http_transport("http://127.0.0.1:1")
    )

    assert result == {
        "ok": False,
        "error": "channel refused the message: channel not found",
    }


def test_http_transport_cannot_route_an_opaque_session(clean_env):
    result = deliver(
        "sub-68bada08", "hi", transport=delivery.http_transport("http://127.0.0.1:1")
    )

    assert result["ok"] is False
    assert "cannot route" in result["error"]


def test_http_transport_needs_a_base_url(clean_env):
    result = deliver("console:default", "hi", transport=delivery.http_transport())

    assert result["ok"] is False
    assert delivery.API_PORT_ENV in result["error"]


# --------------------------------------------------------------------------- #
# async transports
# --------------------------------------------------------------------------- #
def test_deliver_runs_an_async_transport_when_no_loop_is_running(clean_env):
    recorded = []

    async def _transport(session_id, text):
        recorded.append((session_id, text))

    assert deliver("console:default", "hi", transport=_transport) == {"ok": True}
    assert recorded == [("console:default", "hi")]


def test_deliver_asks_for_deliver_async_inside_a_running_loop(clean_env):
    async def _transport(session_id, text):  # pragma: no cover - must not run
        raise AssertionError("should not be awaited by deliver()")

    async def _call():
        return deliver("console:default", "hi", transport=_transport)

    result = asyncio.run(_call())

    assert result["ok"] is False
    assert "deliver_async" in result["error"]


def test_deliver_async_awaits_the_transport(clean_env):
    recorded = []

    async def _transport(session_id, text):
        recorded.append((session_id, text))

    result = asyncio.run(deliver_async("console:default", "hi", transport=_transport))

    assert result == {"ok": True}
    assert recorded == [("console:default", "hi")]


def test_deliver_async_reports_a_failing_transport(clean_env):
    async def _explode(session_id, text):
        raise RuntimeError("loop exploded")

    result = asyncio.run(deliver_async("console:default", "hi", transport=_explode))

    assert result == {"ok": False, "error": "RuntimeError: loop exploded"}


def test_channel_manager_transport_calls_send_text(clean_env):
    seen = {}

    class _Manager:
        async def send_text(self, **kwargs):
            seen.update(kwargs)

    transport = delivery.channel_manager_transport(_Manager(), agent="bot-1")
    result = asyncio.run(deliver_async("wechat:wxid_abc", "hi", transport=transport))

    assert result == {"ok": True}
    assert seen["channel"] == "wechat"
    assert seen["user_id"] == "wxid_abc"
    assert seen["session_id"] == "wechat:wxid_abc"
    assert seen["text"] == "hi"
    assert seen["meta"] == {"_api_send": True, "agent_id": "bot-1"}


def test_channel_manager_transport_rejects_a_bad_session(clean_env):
    class _Manager:
        async def send_text(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("should not be called")

    transport = delivery.channel_manager_transport(_Manager())
    result = asyncio.run(deliver_async("sub-68bada08", "hi", transport=transport))

    assert result["ok"] is False
    assert "cannot route" in result["error"]
