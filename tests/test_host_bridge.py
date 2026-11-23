"""The host's control-command name bridge (US-005 defect D2).

A plugin hands the host a handler with one ``command_name``, and the host
reads it in two conflicting shapes:

* ``qwenpaw/app/_app.py:405-408`` (and ``app/routers/plugins.py:182-185``)
  registers the *channel* key as ``f"/{handler.command_name}"``;
* ``qwenpaw/app/runner/control_commands/__init__.py:62`` registers the
  *dispatch* key as ``handler.command_name.lower()``.

``"/tasks"`` therefore used to land as ``"//tasks"`` in the channel registry
(``channels/base.py:1172`` said "not a command" and the message went through
TaskTracker), while a plain ``"tasks"`` would land as ``"tasks"`` in the
dispatch registry (``runner.query_handler`` would never route it at all).

The first test reproduces the bridge with a stand-in registry so it runs on
any interpreter; the second one repeats it against the **installed host** and
is skipped where ``qwenpaw`` is not importable (i.e. it runs under
``<workspace>\\QwenPaw\\python.exe``).
"""

from __future__ import annotations

import pytest

import prompt_section

#: ``(handler class, slash-free name)`` for every command this plugin ships.
HANDLERS = (
    (prompt_section.TasksCommandHandler, "tasks"),
    (prompt_section.TaskCommandHandler, "task"),
    (prompt_section.NudgeCommandHandler, "nudge"),
)


class FakeChannelRegistry:
    """``qwenpaw/app/channels/command_registry.CommandRegistry``, reduced.

    Same two operations the bridge and ``BaseChannel._consume_one_request``
    use: register a prefix, then ask whether a query starts with one. The
    prefix match duplicates the host rule (a prefix only matches when the
    next character is a space or the end of the query), because ``/tasks``
    matching ``/tasksx`` would be a different bug.
    """

    def __init__(self, defaults: tuple[str, ...] = ("/stop", "/approval")) -> None:
        self._levels = {name: 0 for name in defaults}

    def register_command(self, command_prefix: str, priority_level: int = 10) -> None:
        self._levels[command_prefix.lower()] = priority_level

    def registered(self) -> dict[str, int]:
        return dict(self._levels)

    def is_control_command(self, query: str) -> bool:
        if not query or not isinstance(query, str):
            return False
        query_lower = query.strip().lower()
        if not query_lower.startswith("/"):
            return False
        for prefix in sorted(self._levels, key=len, reverse=True):
            if not query_lower.startswith(prefix):
                continue
            following = query_lower[len(prefix) : len(prefix) + 1]
            if not following or following in (" ", "\t", "\n"):
                return True
        return False


@pytest.mark.parametrize("handler_cls,name", HANDLERS)
def test_command_name_is_slash_free_for_the_bridge(handler_cls, name):
    """``f"/{command_name}"`` must produce the key a channel user types."""

    handler = handler_cls()

    assert handler.command_name == name
    assert f"/{handler.command_name}" == f"/{name}"
    assert "//" not in f"/{handler.command_name}"


@pytest.mark.parametrize("handler_cls,name", HANDLERS)
def test_command_name_lower_is_the_dispatch_key(handler_cls, name):
    """``command_name.lower()`` must be the key the dispatcher looks up."""

    handler = handler_cls()

    assert handler.command_name.lower() == f"/{name}"
    assert type(handler.command_name.lower()) is str


@pytest.mark.parametrize("handler_cls,name", HANDLERS)
def test_app_bridge_registers_the_slashed_channel_key(handler_cls, name):
    """``qwenpaw/app/_app.py:405-408``, replayed against the fake registry."""

    registry = FakeChannelRegistry()
    handler = handler_cls()

    # The host line, verbatim apart from the registry object.
    registry.register_command(f"/{handler.command_name}", priority_level=10)

    assert f"/{name}" in registry.registered()
    assert f"//{name}" not in registry.registered()
    assert registry.is_control_command(f"/{name}") is True
    assert registry.is_control_command(f"/{name} t-20260101-0000-b2c3d4e5 备注") is True
    assert registry.is_control_command(f"/{name}x") is False


@pytest.mark.parametrize("handler_cls,name", HANDLERS)
def test_the_installed_host_agrees_on_both_gates(handler_cls, name):
    """Register the way the host does and ask both gates about the command.

    Skipped unless the real ``qwenpaw`` package is importable, so this is the
    host-interpreter check: channel registry (``channels/base.py:1172``) and
    dispatch registry (``runner/command_dispatch.py:82``).
    """

    control_commands = pytest.importorskip(
        "qwenpaw.app.runner.control_commands",
        reason="host package not importable in this interpreter",
    )
    channels_registry_module = pytest.importorskip(
        "qwenpaw.app.channels.command_registry",
        reason="host package not importable in this interpreter",
    )

    handler = handler_cls()
    channel_registry = channels_registry_module.CommandRegistry()
    channel_registry.register_command(f"/{handler.command_name}", priority_level=10)

    control_commands.register_command(handler)
    try:
        assert channel_registry.is_control_command(f"/{name}") is True
        assert channel_registry.is_control_command(f"/{name} t-1 备注") is True
        assert control_commands.is_control_command(f"/{name}") is True
        assert control_commands.is_control_command(f"/{name} t-1 备注") is True
    finally:
        # The host's own cleanup is asymmetric — ``unregister_command`` does
        # ``command_name.lstrip("/")`` while ``register_command`` keys the
        # registry with the raw name — so it cannot remove a slashed entry.
        # Pop it directly; the test must not leave a handler behind.
        control_commands._COMMAND_REGISTRY.pop(f"/{name}", None)  # noqa: SLF001
