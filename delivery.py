"""Channel delivery for the ``session-task`` plugin (US-003).

One message in, one result out: :func:`deliver` pushes a short text into a
chat session and reports ``{"ok": True}`` / ``{"ok": False, "error": ...}``.
It never raises for a *delivery* failure — a task that finished but could not
be announced must still be recorded as finished — and it never swallows the
cause: the exception text is returned and logged with its traceback.

Where the message actually goes
-------------------------------
Two real send paths exist in QwenPaw 2.3.x, both verified by reading the
installed host, not by guessing:

1. **In process** — ``ChannelManager.send_text(channel=..., user_id=...,
   session_id=..., text=..., meta=...)``
   (``qwenpaw\\app\\channels\\manager.py:813``). This is the call the HTTP
   endpoint below uses internally, so it is the cheapest and the most
   complete path (it keeps the channel's bot prefix and console push store
   behaviour). It is *not* reachable from a plugin through the host's
   context vars — ``qwenpaw.config.context`` only exposes the workspace
   directory and session id — so the object has to be handed in:
   :func:`channel_manager_transport` adapts a ``channel_manager`` into a
   transport and :func:`set_transport` installs it for the process.
2. **Over HTTP** — ``POST /api/messages/send`` with header
   ``X-Agent-Id: <agent>`` and body
   ``{"channel", "target_user", "target_session", "text"}``
   (``qwenpaw\\app\\routers\\messages.py:78``; the CLI's
   ``qwenpaw channels send`` does exactly this,
   ``qwenpaw\\cli\\channels_cmd.py:1213``). :func:`http_transport` builds this
   path; it is used when a base URL can be resolved from the workspace
   ``config.json`` (or ``QWENPAW_API_PORT``).

Session id shape
----------------
Some ids are ``<channel>:<user>`` (``console:default``,
``wechat:wxid_xxx``) — the host builds them as ``f"{channel}:{sender_id}"``
(``qwenpaw\\app\\channels\\base.py:954``). Others are opaque
(``sub-68bada08``). :func:`split_session` only *parses*; a caller whose
transport can route an opaque id (a test double, an in-process adapter that
already knows the channel) does not have to care that parsing failed.

Safety rails
------------
* ``SESSION_TASK_DRY_RUN=1`` skips every transport and returns a dry-run
  record, so the wiring can be exercised on a live host without messaging
  anyone.
* A transport is a callable ``transport(session_id, text) -> None`` and may
  be injected for tests; nothing in this module talks to a channel unless a
  transport resolves.
* The text is never logged in full — only its length and the target.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:  # imported as part of the plugin package
    from . import store
except ImportError:  # imported as a top-level module (tests, plain sys.path)
    import store  # type: ignore[no-redef]

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

__all__ = [
    "AGENT_ID_ENV",
    "API_HOST_ENV",
    "API_PORT_ENV",
    "DRY_RUN_ENV",
    "DRY_RUN_ENV_ALIASES",
    "DRY_RUN_LOG",
    "HTTP_TIMEOUT_SECONDS",
    "MESSAGES_ENDPOINT",
    "CONSOLE_CHAT_ENDPOINT",
    "OUTBOX_DIRNAME",
    "DeliveryError",
    "Transport",
    "agent_id",
    "api_base",
    "channel_manager_transport",
    "default_transport",
    "deliver",
    "deliver_async",
    "dry_run_log_path",
    "get_transport",
    "http_transport",
    "set_transport",
    "split_session",
]

#: ``<scheme>://<host>:<port>`` is assembled from these two env vars.
API_PORT_ENV = "QWENPAW_API_PORT"
API_HOST_ENV = "QWENPAW_API_HOST"
#: Agent id for the ``X-Agent-Id`` header (defaults to the workspace name).
AGENT_ID_ENV = "QWENPAW_AGENT_ID"
#: ``1`` / ``true`` / ``yes`` disables every transport.
DRY_RUN_ENV = "SESSION_TASK_DRY_RUN"
#: Aliases that also switch the dry run on. ``QWENPAW_TASK_DRY_RUN`` is what
#: the smoke-test harness sets; the plugin's own name stays canonical so an
#: operator reading the logs finds one variable to explain.
DRY_RUN_ENV_ALIASES: Tuple[str, ...] = (DRY_RUN_ENV, "QWENPAW_TASK_DRY_RUN")

#: Where dry-run messages are written so a smoke test can assert what *would*
#: have been sent. Inside the task data directory, next to the event logs.
OUTBOX_DIRNAME = "outbox"
DRY_RUN_LOG = "dry-run.jsonl"

#: Host HTTP endpoint for proactive text sends.
MESSAGES_ENDPOINT = "/api/messages/send"
CONSOLE_CHAT_ENDPOINT = "/api/console/chat"
#: Host used when ``config.json`` only names a port.
DEFAULT_HOST = "127.0.0.1"
HTTP_TIMEOUT_SECONDS = 10.0

#: Any callable with this shape can act as a transport. It may return an
#: awaitable (the in-process channel API is ``async``).
Transport = Callable[[str, str], Any]


class DeliveryError(RuntimeError):
    """A message could not be routed or the host refused to send it."""


#: Installed in-process transport (``set_transport``). ``None`` means "not
#: wired yet", which is the normal state until the host integration runs.
_INPROC_TRANSPORT: Optional[Transport] = None


# --------------------------------------------------------------------------- #
# Session ids
# --------------------------------------------------------------------------- #
def split_session(session_id: Any) -> Optional[Tuple[str, str]]:
    """Split ``"<channel>:<user>"`` into ``(channel, user)``, else ``None``.

    ``None`` covers everything a transport cannot route by itself: a non
    string, an empty string, a missing separator, or an empty side of it.
    Colons *inside* the user part are kept (``"qq:a:b"`` → ``("qq", "a:b")``)
    because only the first separator is structural.
    """

    if not isinstance(session_id, str):
        return None
    text = session_id.strip()
    if not text:
        return None
    channel, separator, user = text.partition(":")
    if not separator:
        return None
    channel = channel.strip()
    user = user.strip()
    if not channel or not user:
        return None
    return channel, user


# --------------------------------------------------------------------------- #
# Host wiring
# --------------------------------------------------------------------------- #
def _workspace_dir() -> Any:
    """Workspace root, resolved exactly like the event store does."""

    return store.workspace_root()


def api_base() -> Optional[str]:
    """Base URL of the local QwenPaw API, or ``None`` when unknown.

    Resolution order:

    1. ``QWENPAW_API_PORT`` (+ optional ``QWENPAW_API_HOST``);
    2. ``<workspace>/config.json`` → ``last_api.port`` (what the running host
       actually writes: ``{"last_api": {"host": "127.0.0.1", "port": 64447}}``);
    3. the same file's top-level ``port`` / ``host`` (older layouts);
    4. ``None`` — no guess, because a wrong port would turn a delivery into a
       confusing connection error instead of "not configured".

    The port itself is never defaulted to 8088: that is only the CLI's
    fallback, and a host that moved would be silently misdelivered.
    """

    port = os.environ.get(API_PORT_ENV, "").strip()
    host = os.environ.get(API_HOST_ENV, "").strip()

    if not port:
        config = _read_config()
        last_api = config.get("last_api")
        if isinstance(last_api, dict):
            port = str(last_api.get("port") or "").strip()
            host = host or str(last_api.get("host") or "").strip()
        if not port:
            port = str(config.get("port") or "").strip()
            host = host or str(config.get("host") or "").strip()

    if not port:
        # The desktop launcher writes the port it listened on to
        # ``<home>/.copaw/desktop_port``; a plugin runs inside that process and
        # inherits no env var pointing at it, so this file is the one source
        # that is always present on a live desktop host.
        for name in ("desktop_port", "api_port"):
            try:
                path = os.path.join(
                    os.environ.get("QWENPAW_HOME") or os.path.expanduser("~"),
                    ".copaw",
                    name,
                )
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8", errors="replace") as handle:
                    value = (handle.readline() or "").strip()
            except OSError:
                continue
            if value.isdigit():
                port = value
                host = host or DEFAULT_HOST
                break

    if not port:
        return None
    try:
        number = int(port)
    except (TypeError, ValueError):
        logger.warning("ignoring non-numeric API port %r", port)
        return None
    if not 0 < number < 65536:
        logger.warning("ignoring out-of-range API port %r", port)
        return None
    return f"http://{host or DEFAULT_HOST}:{number}"


def _read_config() -> dict:
    """Read ``<workspace>/config.json``; any problem yields ``{}``."""

    try:
        path = _workspace_dir() / "config.json"
    except Exception:  # pragma: no cover - unresolvable workspace
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_id() -> str:
    """Agent id for the ``X-Agent-Id`` header.

    ``QWENPAW_AGENT_ID`` wins; otherwise the workspace directory name (each
    agent owns one workspace directory, e.g. ``...\\default``); otherwise
    ``"default"``, which is also what the host assumes when the header is
    missing (``app/routers/messages.py:114``).
    """

    env = os.environ.get(AGENT_ID_ENV, "").strip()
    if env:
        return env
    try:
        name = _workspace_dir().name
    except Exception:  # pragma: no cover - unruly path
        return "default"
    # A workspace that is not an agent directory (the host's own ``.copaw``
    # root, an empty name) must not become an agent id: the console chat
    # endpoint resolves agents by this value and answers 404 for an unknown
    # one, which looks exactly like "route missing".
    if not name or name.startswith("."):
        return "default"
    return name


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def http_transport(
    base_url: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Transport:
    """Build a transport that POSTs :data:`MESSAGES_ENDPOINT`.

    Args:
        base_url: ``http://host:port``; resolved with :func:`api_base` when
            omitted.
        agent: value for ``X-Agent-Id``; defaults to :func:`agent_id`.
        timeout: socket timeout in seconds.
    """

    def _transport(session_id: str, text: str) -> None:
        target = split_session(session_id)
        if target is None:
            raise DeliveryError(
                f"cannot route session {session_id!r}: expected '<channel>:<user>'"
            )
        channel, user = target
        base = (base_url or api_base() or "").rstrip("/")
        if not base:
            raise DeliveryError(
                f"no API base url: set {API_PORT_ENV} or write last_api.port to config.json"
            )

        body = json.dumps(
            {
                "channel": channel,
                "target_user": user,
                # Keep the composite id: the console channel pushes through a
                # store keyed by the session id the host itself generated.
                "target_session": session_id,
                "text": text,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        request = Request(f"{base}{MESSAGES_ENDPOINT}", data=body, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("X-Agent-Id", agent or agent_id())

        try:
            with urlopen(request, timeout=timeout or HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except HTTPError as exc:
            raise DeliveryError(
                f"HTTP {exc.code} from {MESSAGES_ENDPOINT}: {exc.reason}"
            ) from exc
        except URLError as exc:
            raise DeliveryError(f"cannot reach {base}: {exc.reason}") from exc

        if isinstance(status, int) and status >= 400:
            raise DeliveryError(f"HTTP {status} from {MESSAGES_ENDPOINT}")

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise DeliveryError(
                f"channel refused the message: {payload.get('message') or 'unknown reason'}"
            )

    return _transport


def console_chat_transport(
    base_url: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Transport:
    """Build a transport that starts a turn in a console/agent session.

    ``POST /api/messages/send`` only hands text to a channel; for an agent
    session it does not start a turn, so a delegated task would sit unseen
    until the target session next ran for some other reason (minutes). The
    console chat endpoint does start one: it attaches to the chat's task
    tracker (or starts it) and streams the reply, which keeps running in the
    background after we close the socket
    (``qwenpaw\\app\\routers\\console.py:135``).

    Body shape is what the host parses for a dict request:
    ``{"channel", "user_id", "session_id", "input": [{"content": [...]}]}``.
    """

    def _transport(session_id: str, text: str) -> None:
        base = (base_url or api_base() or "").rstrip("/")
        if not base:
            raise DeliveryError(
                f"no API base url: set {API_PORT_ENV} or write last_api.port to config.json"
            )
        owner = agent or agent_id()
        body = json.dumps(
            {
                "channel": "console",
                "user_id": owner,
                "session_id": session_id,
                "input": [
                    {"content": [{"type": "text", "text": text}]},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{base}{CONSOLE_CHAT_ENDPOINT}", data=body, method="POST"
        )
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("X-Agent-Id", owner)
        try:
            with urlopen(request, timeout=timeout or HTTP_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                # The endpoint answers with an SSE stream that continues in the
                # background; read one byte so the turn is attached, then close.
                response.read(1)
        except HTTPError as exc:
            raise DeliveryError(
                f"HTTP {exc.code} from {CONSOLE_CHAT_ENDPOINT}: {exc.reason}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise DeliveryError(
                f"cannot reach {base}{CONSOLE_CHAT_ENDPOINT}: {getattr(exc, 'reason', exc)}"
            ) from exc

        if isinstance(status, int) and status >= 400:
            raise DeliveryError(f"HTTP {status} from {CONSOLE_CHAT_ENDPOINT}")

    return _transport


def channel_manager_transport(
    channel_manager: Any, *, agent: Optional[str] = None
) -> Transport:
    """Adapt a host ``ChannelManager`` into a transport (in-process path).

    The returned callable is ``async``: ``ChannelManager.send_text`` is a
    coroutine, and awaiting it keeps the send on the host's event loop. Call
    it through :func:`deliver_async` (or :func:`deliver`, which runs the
    coroutine itself when there is no loop running).
    """

    async def _transport(session_id: str, text: str) -> None:
        target = split_session(session_id)
        if target is None:
            raise DeliveryError(
                f"cannot route session {session_id!r}: expected '<channel>:<user>'"
            )
        channel, user = target
        meta: dict = {"_api_send": True}
        if agent:
            meta["agent_id"] = agent
        await channel_manager.send_text(
            channel=channel,
            user_id=user,
            session_id=session_id,
            text=text,
            meta=meta,
        )

    return _transport


def set_transport(transport: Optional[Transport]) -> None:
    """Install (or clear, with ``None``) the in-process transport."""

    global _INPROC_TRANSPORT
    if transport is not None and not callable(transport):
        raise TypeError("transport must be callable")
    _INPROC_TRANSPORT = transport
    logger.info(
        "session-task: in-process transport %s",
        "installed" if transport is not None else "cleared",
    )


def get_transport() -> Optional[Transport]:
    """The transport installed with :func:`set_transport`, if any."""

    return _INPROC_TRANSPORT


def routed_transport(
    base_url: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Optional[Transport]:
    """Pick ``/api/console/chat`` for agent sessions, ``/api/messages/send`` otherwise.

    ``<channel>:<user>`` ids belong to a channel; everything else (a bare
    session id or a chat id) is an agent session and needs a real turn.
    """

    base = (base_url or api_base() or "").rstrip("/")
    if not base:
        return None
    chat = console_chat_transport(base, agent=agent, timeout=timeout)
    message = http_transport(base, agent=agent, timeout=timeout)

    def _transport(session_id: str, text: str) -> None:
        target = split_session(session_id)
        if target is not None and target[0] != "console":
            return message(session_id, text)
        return chat(session_id, text)

    return _transport


def default_transport() -> Optional[Transport]:
    """Pick a transport: in-process first, routed HTTP second, ``None`` last."""

    if _INPROC_TRANSPORT is not None:
        return _INPROC_TRANSPORT
    return routed_transport()


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _dry_run_enabled() -> bool:
    for name in DRY_RUN_ENV_ALIASES:
        if os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def dry_run_log_path() -> Any:
    """Path of the dry-run outbox, or ``None`` when the workspace is unknown."""

    try:
        return (
            _workspace_dir() / store.TASKS_DIRNAME / OUTBOX_DIRNAME / DRY_RUN_LOG
        )
    except Exception:  # pragma: no cover - unresolvable workspace
        logger.warning("session-task: cannot resolve the dry-run outbox", exc_info=True)
        return None


def _record_dry_run(target: str, text: str) -> None:
    """Append ``{"at", "target", "text"}`` to the dry-run outbox.

    Best effort by design: a dry run exists to observe a message, so failing
    to write the observation must not turn into a failed delivery. The record
    still carries ``last``/``index``-free plain JSONL, one line per message,
    so a smoke test can read the exact text back.
    """

    path = dry_run_log_path()
    if path is None:
        return
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": target,
        "chars": len(text),
        "text": text,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("session-task: could not record the dry-run message", exc_info=True)


def _validate(session_id: Any, text: Any) -> Tuple[str, str]:
    """Return ``(session_id, text)`` or raise :class:`DeliveryError`."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise DeliveryError("session_id must be a non-empty string")
    if not isinstance(text, str):
        raise DeliveryError("text must be a string")
    if not text.strip():
        raise DeliveryError("text must not be empty")
    return session_id.strip(), text


def _failure(exc: BaseException, target: Any, *, exc_info: bool = False) -> dict:
    logger.warning("deliver to %s failed: %s", target, exc, exc_info=exc_info)
    return {"ok": False, "error": str(exc)}


def _dry_run_result(target: str, text: str) -> dict:
    logger.info("deliver to %s skipped (%s=1)", target, DRY_RUN_ENV)
    _record_dry_run(target, text)
    return {"ok": True, "dry_run": True, "target": target, "chars": len(text)}


def deliver(
    session_id: str, text: str, *, transport: Optional[Transport] = None
) -> dict:
    """Send ``text`` to ``session_id`` and report what happened.

    Args:
        session_id: target session, e.g. ``"console:default"``. An opaque id
            is accepted here; the transport decides whether it can route it.
        text: the message body (the initiator summary, the task card).
        transport: injectable callable ``transport(session_id, text)``; used
            by tests. Defaults to :func:`default_transport`, which prefers the
            in-process channel API and then the host HTTP API.

    Returns:
        dict: ``{"ok": True}`` on success, ``{"ok": True, "dry_run": True,
        "target": ..., "chars": ...}`` when ``SESSION_TASK_DRY_RUN`` is on,
        and ``{"ok": False, "error": "<reason>"}`` otherwise. Delivery
        failures never raise — the caller is usually finishing a task and
        must not lose that record because a channel was down.
    """

    try:
        target, body = _validate(session_id, text)
    except DeliveryError as exc:
        return _failure(exc, session_id)

    if _dry_run_enabled():
        return _dry_run_result(target, body)

    send = transport if transport is not None else default_transport()
    if send is None:
        return _failure(DeliveryError("no transport available"), target)

    try:
        outcome = send(target, body)
        if inspect.isawaitable(outcome):
            _run_awaitable(outcome)
    except DeliveryError as exc:
        return _failure(exc, target)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.warning("deliver to %s crashed", target, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    logger.info("deliver ok: %s (%d chars)", target, len(body))
    return {"ok": True}


async def deliver_async(
    session_id: str, text: str, *, transport: Optional[Transport] = None
) -> dict:
    """:func:`deliver` for callers that already run on an event loop.

    Same contract and same result dict; an ``async`` transport (the in-process
    channel API) is awaited instead of being run in a new loop, which is what
    a tool handler inside the host needs.
    """

    try:
        target, body = _validate(session_id, text)
    except DeliveryError as exc:
        return _failure(exc, session_id)

    if _dry_run_enabled():
        return _dry_run_result(target, body)

    send = transport if transport is not None else default_transport()
    if send is None:
        return _failure(DeliveryError("no transport available"), target)

    try:
        if inspect.iscoroutinefunction(send):
            await send(target, body)
        else:
            # A blocking transport (urllib) would freeze the host's event loop
            # when a tool handler awaits delivery — the API request it is
            # making could never be answered by that same loop. Run it in a
            # worker thread and keep the loop free.
            outcome = await asyncio.to_thread(send, target, body)
            if inspect.isawaitable(outcome):
                await outcome
    except DeliveryError as exc:
        return _failure(exc, target)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        logger.warning("deliver to %s crashed", target, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    logger.info("deliver ok: %s (%d chars)", target, len(body))
    return {"ok": True}


def _run_awaitable(awaitable: Any) -> Any:
    """Run an ``async`` transport from synchronous code, or refuse clearly.

    ``asyncio.run`` is only safe when this thread has no running loop; inside
    one it would raise ``RuntimeError`` *after* the coroutine was created, so
    the coroutine is closed explicitly to avoid a "never awaited" warning.
    Callers in an async context should use :func:`deliver_async`.
    """

    if not inspect.iscoroutine(awaitable):
        raise DeliveryError("async transport returned a non-coroutine awaitable")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    awaitable.close()
    raise DeliveryError(
        "deliver() cannot await inside a running loop; use deliver_async()"
    )
