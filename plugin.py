"""``session-task`` plugin entry point (US-003, extended in US-005 / US-006).

Registered with the real QwenPaw 2.3 loader contract
(``D:\\QwenPaw\\Lib\\site-packages\\qwenpaw\\plugins\\loader.py``):

* the module must export ``plugin``;
* the loader calls ``plugin.register(api)`` (sync or async);
* ``api.register_tool(tool_name=..., tool_func=..., description=...,
  icon=..., enabled=...)`` is the supported way to add an agent tool
  (``qwenpaw/plugins/api.py``). It defers the actual registration to a
  startup hook that puts the function on ``qwenpaw.agents.tools`` and adds a
  ``BuiltinToolConfig`` entry for the agent.

Two consequences worth stating out loud:

* the LLM-visible name and JSON schema come from the **function itself**
  (``__name__``, type hints, Google-style docstring), not from
  ``description=``. That argument only feeds the tool-management UI — which
  is why the docstrings in ``tools.py`` carry the trigger words too.
* ``enabled=True`` is intentional. These six tools *are* the plugin; leaving
  them disabled would make a fresh install a no-op until somebody toggles
  each one in the UI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Tuple

try:  # loaded as a package by the host loader
    from . import http_api as _http_api
    from . import prompt_section as _prompt_section
    from . import reliability as _reliability
    from . import tools as _tools
except ImportError:  # loaded as a top-level module (tests, plain sys.path)
    import http_api as _http_api  # type: ignore[no-redef]
    import prompt_section as _prompt_section  # type: ignore[no-redef]
    import reliability as _reliability  # type: ignore[no-redef]
    import tools as _tools  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

#: This plugin's own directory — the base for the bundled skill provider.
PLUGIN_DIR = Path(__file__).resolve().parent

#: ``(tool_name, tool_func, description, icon)`` in registration order.
#: Descriptions are Chinese and repeat the trigger words the docstrings use,
#: so the UI shows the same "when to call me" hint the model sees.
TOOL_SPECS: Tuple[Tuple[str, Any, str, str], ...] = (
    (
        "task_add",
        _tools.task_add,
        "把任务委派给另一个会话并登记（委派 / 派给 / 交给 / 转给 / 新建任务）",
        "📥",
    ),
    (
        "task_list",
        _tools.task_list,
        "列出当前会话的任务摘要（任务列表 / 有哪些任务 / 看看进度）",
        "🗂️",
    ),
    (
        "task_get",
        _tools.task_get,
        "查看单个任务的完整视图与历史（任务详情 / 查任务 / 这个任务到哪了）",
        "🔍",
    ),
    (
        "task_report",
        _tools.task_report,
        "汇报任务进度或结果并推进状态（汇报 / 完成 / 卡住 / 失败 / 更新状态）",
        "📝",
    ),
    (
        "task_pulse",
        _tools.task_pulse,
        "给进行中的任务打心跳，表示还在推进（心跳 / 还在做 / 打个点）",
        "💓",
    ),
    (
        "task_cancel",
        _tools.task_cancel,
        "取消任务并记下原因（取消 / 撤销 / 不做了 / 终止任务）",
        "🚫",
    ),
)


class SessionTaskPlugin:
    """Registers the delegation tools, prompt section, commands and HTTP API."""

    def register(self, api: Any) -> None:
        """Register every surface this plugin owns.

        The six tools are mandatory (``register_tool`` has been part of the
        plugin API since 2.3). The US-005 / US-006 surfaces are optional: each
        is reached through ``getattr`` and skipped with a warning when the
        installed host does not expose it, so an older host loads the plugin
        with six tools instead of failing to load at all.

        Args:
            api: ``qwenpaw.plugins.api.PluginApi`` instance provided by the
                host loader.
        """

        for tool_name, tool_func, description, icon in TOOL_SPECS:
            api.register_tool(
                tool_name=tool_name,
                tool_func=tool_func,
                description=description,
                icon=icon,
                enabled=True,
            )
            logger.info("session-task: registered tool '%s'", tool_name)

        logger.info("session-task: %d delegation tools registered", len(TOOL_SPECS))

        self._register_skills(api)
        self._register_prompt_section(api)
        self._register_commands(api)
        self._register_http_router(api)
        self._register_reliability(api)

    # ------------------------------------------------------------------ #
    # Reliability layer (US-010)
    # ------------------------------------------------------------------ #
    def _register_reliability(self, api: Any) -> None:
        """Attach the turn-end pulse hook / stop guard when the host has them.

        QwenPaw 2.3.0 exposes neither ``register_runtime_hook`` nor
        ``register_agent_stop_handler`` (see ``reliability.py``), so this is a
        warning plus a no-op there — and one line of work the day upstream
        ships them. A failure here must never cost the six tools.
        """

        try:
            result = _reliability.install(api)
        except Exception:  # noqa: BLE001 - optional surface, never fatal
            logger.warning(
                "session-task: reliability layer not installed", exc_info=True
            )
            return

        logger.info(
            "session-task: reliability installed=%s missing=%s",
            result.get("installed", []),
            result.get("missing", []),
        )

    # ------------------------------------------------------------------ #
    # Bundled skill (US-007)
    # ------------------------------------------------------------------ #
    def _register_skills(self, api: Any) -> None:
        """Ship the ``session-task-playbook`` skill with the plugin.

        ``register_skill_provider(skills_dir, *, enabled_by_default=True,
        channels=None)`` (``qwenpaw/plugins/api.py:514``) copies every
        ``<skills_dir>/<name>/SKILL.md`` into each workspace, reconciles the
        workspace skill manifest and removes the entries again on uninstall.
        It is newer than ``register_tool``, so it is reached through
        ``getattr``: an older host keeps the six tools and loses only the
        skill, instead of failing to load the plugin.
        """

        register = getattr(api, "register_skill_provider", None)
        if register is None:
            logger.warning(
                "session-task: host has no register_skill_provider; "
                "the session-task-playbook skill will not be installed"
            )
            return

        skills_dir = PLUGIN_DIR / "skills"
        if not skills_dir.is_dir():
            logger.warning(
                "session-task: bundled skills directory missing (%s)", skills_dir
            )
            return

        try:
            register(skills_dir=skills_dir, enabled_by_default=True)
        except TypeError:
            # Signature drift: the two-argument form is what the docstring
            # shows positionally, so it is the best second guess.
            try:
                register(skills_dir, True)
            except Exception:  # noqa: BLE001 - a missing skill is not fatal
                logger.warning(
                    "session-task: could not register the skill provider",
                    exc_info=True,
                )
                return
        except Exception:  # noqa: BLE001 - a missing skill is not fatal
            logger.warning(
                "session-task: could not register the skill provider",
                exc_info=True,
            )
            return

        logger.info("session-task: skill provider registered (%s)", skills_dir)

    # ------------------------------------------------------------------ #
    # Optional surfaces (US-005 / US-006)
    # ------------------------------------------------------------------ #
    def _register_prompt_section(self, api: Any) -> None:
        """Register the dynamic task digest, downgrading the anchor if needed.

        The story asks for ``after="workspace/AGENTS.md"``; the installed host
        validates the anchor against ``PromptBuilder.HOST_ANCHORS``
        (``workspace`` / ``multimodal`` / ``env_context``) and raises
        ``ValueError`` for anything else. Rather than guess, try the requested
        anchor first and fall back to ``workspace`` — the section then lands
        right after the AGENTS.md block anyway, because that text *is* the
        ``workspace`` anchor.
        """

        register = getattr(api, "register_prompt_section", None)
        if register is None:
            logger.warning(
                "session-task: host has no register_prompt_section; "
                "the live task digest will not be injected"
            )
            return

        for anchor in (
            _prompt_section.SECTION_ANCHOR,
            _prompt_section.SECTION_FALLBACK_ANCHOR,
        ):
            try:
                register(
                    name=_prompt_section.SECTION_NAME,
                    provider=_prompt_section.provider,
                    after=anchor,
                )
            except ValueError as exc:
                logger.warning(
                    "session-task: prompt section anchor %r rejected (%s)",
                    anchor,
                    exc,
                )
                continue
            except Exception:  # noqa: BLE001 - a nice-to-have must not break load
                logger.warning(
                    "session-task: could not register the prompt section",
                    exc_info=True,
                )
                return
            else:
                logger.info(
                    "session-task: prompt section '%s' registered after %r",
                    _prompt_section.SECTION_NAME,
                    anchor,
                )
                return

        logger.warning("session-task: no acceptable prompt anchor; digest disabled")

    def _register_commands(self, api: Any) -> None:
        """Register ``/tasks``, ``/task`` and ``/nudge``.

        ``register_control_command`` is the only command channel the host
        exposes on ``PluginApi`` (``plugins/api.py:302``); there is no
        ``register_command`` / ``register_slash`` in the plugin API, which the
        report for US-005 records with file and line numbers.
        """

        register = getattr(api, "register_control_command", None)
        if register is None:
            logger.warning(
                "session-task: host has no register_control_command; "
                "/tasks /task /nudge unavailable"
            )
            return

        for handler_cls in _prompt_section.COMMAND_HANDLERS:
            handler = handler_cls()
            try:
                register(handler=handler)
            except Exception:  # noqa: BLE001 - one bad command, not a dead plugin
                logger.warning(
                    "session-task: could not register command %r",
                    handler.command_name,
                    exc_info=True,
                )
                continue
            logger.info("session-task: command %s registered", handler.command_name)

    def _register_http_router(self, api: Any) -> None:
        """Mount the read-only task API under ``/api/session-task``."""

        register = getattr(api, "register_http_router", None)
        if register is None:
            logger.warning(
                "session-task: host has no register_http_router; "
                "GET /api/session-task/tasks unavailable"
            )
            return

        try:
            router = _http_api.build_router()
        except Exception:  # noqa: BLE001 - e.g. FastAPI not installed
            logger.warning(
                "session-task: could not build the HTTP router",
                exc_info=True,
            )
            return

        try:
            register(router=router, prefix=_http_api.ROUTE_PREFIX)
        except Exception:  # noqa: BLE001 - duplicate prefix on reload, etc.
            logger.warning(
                "session-task: could not register the HTTP router",
                exc_info=True,
            )
            return
        logger.info(
            "session-task: HTTP routes registered under /api%s",
            _http_api.ROUTE_PREFIX,
        )


#: Exported object the host loader looks up (``module.plugin``).
plugin = SessionTaskPlugin()

__all__ = ["PLUGIN_DIR", "TOOL_SPECS", "SessionTaskPlugin", "plugin"]
