"""Durable, append-only event store for the ``session-task`` plugin.

Layout (all under the current workspace)::

    <workspace>/session-tasks/events/<task_id>.jsonl   # append-only event log
    <workspace>/session-tasks/index/<session_id>.json  # materialised view (later)
    <workspace>/session-tasks/detail/<task_id>.json    # detached detail (later)
    <workspace>/session-tasks/.locks/<task_id>.lock    # write lock

Design notes
------------
* One JSONL file per task; appends only, never rewrite.
* Writes are serialised with an ``O_CREAT | O_EXCL`` lock file so two
  sessions (or two threads) cannot interleave partial lines.
* Lock acquisition treats *every* "the lock is momentarily unusable" error
  as retryable. On Windows a lock file that is being deleted is briefly
  un-creatable (``EACCES`` / ``PermissionError``); retrying only on
  ``FileExistsError`` silently loses events under load.
* Every record carries a monotonically increasing ``seq`` and an ISO8601
  ``at`` timestamp. ``seq`` is computed *inside* the lock.
* A torn trailing line (crash between write and newline) is healed before
  the next append by writing the missing ``b"\n"`` first, so the new record
  is never glued onto a broken line.
* Reads never raise on bad data: a corrupt line is logged and skipped so a
  single truncated write cannot make a task unreadable. That includes
  ``NaN`` / ``Infinity`` (literal spellings Python accepts but JSON does not)
  and numbers that overflow on parse such as ``1e309``: ``json.loads`` turns
  the latter into ``inf`` without ever calling the ``parse_constant`` hook, so
  the parsed record is checked for non-finite floats at any depth.
* ``seq`` comes from the highest integer ``seq`` already on disk, never from
  the number of readable records: a skipped line or a legacy record without
  ``seq`` must not open a gap or a repeat.
* Task ids carry 8 random hex digits, so two tasks created in the same
  minute cannot share a file (and therefore cannot share a history).

The module deliberately has no hard dependency on the QwenPaw host package:
it works inside the host (``qwenpaw.config.context``), in a plain test
process (``QWENPAW_WORKING_DIR``), or failing both, in the cwd.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import json
import logging
import math
import os
import random
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger("qwenpaw").getChild("plugin.session-task")

#: Environment variable used when the QwenPaw host context is unavailable.
WORKSPACE_ENV = "QWENPAW_WORKING_DIR"
TASKS_DIRNAME = "session-tasks"
EVENTS_DIRNAME = "events"
INDEX_DIRNAME = "index"
DETAIL_DIRNAME = "detail"
LOCK_DIRNAME = ".locks"

#: Lock acquisition: up to 400 attempts * ~10 ms (+ jitter) ≈ 4-6 s before
#: giving up. The budget has to absorb a queue of concurrent writers, not
#: just a single competitor.
LOCK_RETRIES = 400
LOCK_SLEEP_SECONDS = 0.01
LOCK_SLEEP_JITTER_SECONDS = 0.005
#: Unlink retries when Windows reports a transient sharing violation while
#: releasing the lock; a lingering lock file blocks every later writer.
LOCK_RELEASE_RETRIES = 20
LOCK_RELEASE_SLEEP_SECONDS = 0.005
#: A lock file older than this is treated as left behind by a crashed writer.
LOCK_STALE_SECONDS = 30.0

#: ``os.open`` errors meaning "the lock is momentarily unusable, try again".
_TRANSIENT_LOCK_ERRNOS = frozenset(
    {
        errno.EACCES,  # Windows: delete-pending file or sharing violation
        errno.EPERM,
        errno.EBUSY,
        errno.EEXIST,  # the lock is currently held
        errno.EDEADLK,
    }
)
#: Windows equivalents of the above (``winerror`` is not always mirrored in
#: ``errno`` by CPython).
_TRANSIENT_WINERRORS = frozenset({5, 32, 33, 183})

#: Characters allowed in a task id (it is used as a file name).
_TASK_ID_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

#: Windows resolves these names to devices no matter the extension, so
#: ``nul.jsonl`` writes to a bit bucket and ``nul.lock`` can never be removed.
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

#: Guards the process-local set of ids issued during the current minute.
_ID_GUARD = threading.Lock()
_id_stamp = ""
_issued_ids: set[str] = set()

__all__ = [
    "WORKSPACE_ENV",
    "agent_workspace_for",
    "append_event",
    "candidate_roots",
    "detail_dir",
    "events_dir",
    "events_dir_at",
    "has_events_at",
    "index_dir",
    "new_task_id",
    "read_events",
    "task_path",
    "tasks_dir",
    "tasks_path_at",
    "validate_task_id",
    "workspace_root",
    "workspace_root_for",
    "workspace_scope",
]


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _host_workspace_dir() -> Optional[Path]:
    """Return the workspace directory from the QwenPaw host, if available.

    Kept as a separate function so unit tests can monkeypatch it to ``None``
    and exercise the environment-variable fallback even when the host package
    happens to be importable in the test interpreter.
    """

    try:  # pragma: no cover - depends on host installation
        from qwenpaw.config.context import get_current_workspace_dir
    except Exception:  # ImportError, but tolerate any host-side failure
        return None

    try:
        value = get_current_workspace_dir()
    except Exception:  # no active context (e.g. background thread)
        logger.debug("get_current_workspace_dir() raised; falling back", exc_info=True)
        return None

    if not value:
        return None
    return Path(value)


#: Pinned workspace for the current task. A host **HTTP request** carries no
#: agent in its context, so :func:`workspace_root` there degrades to the host
#: process cwd (``C:\Users\<user>\.copaw``) and every task written by
#: the agent tools becomes invisible. A router pins the right root here with
#: :func:`workspace_scope` instead of forking a second store implementation.
_WORKSPACE_OVERRIDE: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "session_task_workspace_override", default=None
)


def _host_agent_workspaces() -> dict[str, Path]:
    """``{agent_id: workspace_dir}`` from the host config, ``{}`` when unknown.

    Read-only and lazily imported: the module must stay usable in a plain test
    interpreter (no ``qwenpaw`` on ``sys.path``) and a broken/absent host
    config must degrade to the old behaviour, never raise.
    """

    try:  # pragma: no cover - depends on host installation
        from qwenpaw.config import load_config
    except Exception:  # ImportError, or a host that moved the function
        return {}

    try:  # pragma: no cover - depends on host installation
        profiles = load_config().agents.profiles
        workspaces: dict[str, Path] = {}
        for agent_id, profile in profiles.items():
            workspace_dir = getattr(profile, "workspace_dir", None)
            if workspace_dir:
                workspaces[str(agent_id)] = Path(workspace_dir).expanduser()
        return workspaces
    except Exception:  # pragma: no cover - malformed config
        logger.debug("could not read agent workspaces from host config", exc_info=True)
        return {}


def _agent_id_candidates(session_id: str) -> list[str]:
    """Agent ids ``session_id`` may belong to, most specific first.

    Sessions are named ``<agent>/<session>`` or ``<agent>:<session>`` in some
    deployments and by a bare agent id in others, so the head segment is tried
    too. The whole id is checked first so an agent literally called ``a:b``
    still wins over the split form.
    """

    candidates = [session_id]
    for separator in ("/", ":"):
        head, found, _ = session_id.partition(separator)
        if found and head and head not in candidates:
            candidates.append(head)
    return candidates


def agent_workspace_for(session_id: Optional[str] = None) -> Optional[Path]:
    """Workspace configured for the agent behind ``session_id``, if any.

    ``None`` when the host config is unavailable or no agent matches (a bare
    ``console:<session>`` id belongs to no agent by name).
    """

    if not session_id:
        return None
    workspaces = _host_agent_workspaces()
    if not workspaces:
        return None
    for candidate in _agent_id_candidates(session_id):
        root = workspaces.get(candidate)
        if root is not None:
            return root
    return None


def workspace_root_for(session_id: Optional[str] = None) -> Path:
    """Resolve the workspace for one session: agent config → env → cwd.

    :func:`workspace_root` is deliberately left alone (tools rely on it); this
    is the richer lookup for callers that *do* know the session and therefore
    may be able to recover the agent's real workspace from the host config.
    """

    root = agent_workspace_for(session_id)
    if root is not None:
        return root

    env_value = os.environ.get(WORKSPACE_ENV)
    if env_value:
        return Path(env_value)

    return workspace_root()


def candidate_roots(session_id: Optional[str] = None) -> list[Path]:
    """Roots worth probing for a session, best guess first, de-duplicated.

    Last are every configured agent workspace: when neither the session nor
    the process cwd knows the answer, the task log itself is the oracle, and
    there are only a handful of agents.
    """

    roots: list[Path] = []
    for root in (
        workspace_root_for(session_id),
        workspace_root(),
        *_host_agent_workspaces().values(),
    ):
        root = Path(root)
        if root not in roots:
            roots.append(root)
    return roots


@contextlib.contextmanager
def workspace_scope(root: Path | str) -> Iterator[Path]:
    """Run the block with :func:`workspace_root` pinned to ``root``.

    Used by the HTTP layer to read one candidate workspace (and by tests to
    point the store at a temporary directory).
    """

    pinned = Path(root)
    token = _WORKSPACE_OVERRIDE.set(pinned)
    try:
        yield pinned
    finally:
        _WORKSPACE_OVERRIDE.reset(token)


def workspace_root() -> Path:
    """Resolve the workspace root: pinned scope → host context → env → cwd."""

    pinned = _WORKSPACE_OVERRIDE.get()
    if pinned is not None:
        return pinned

    root = _host_workspace_dir()
    if root is not None:
        return root

    env_value = os.environ.get(WORKSPACE_ENV)
    if env_value:
        return Path(env_value)

    return Path.cwd()


def events_dir_at(root: Path | str) -> Path:
    """``<root>/session-tasks/events`` **without** creating anything.

    Probing a candidate root must be side-effect free: creating
    ``session-tasks/`` inside every agent workspace just to find out whether a
    task lives there would litter the host.
    """

    return Path(root) / TASKS_DIRNAME / EVENTS_DIRNAME


def tasks_path_at(root: Path | str) -> Path:
    """``<root>/session-tasks`` without creating anything."""

    return Path(root) / TASKS_DIRNAME


def has_events_at(root: Path | str) -> bool:
    """True when ``root`` already holds at least one task log."""

    try:
        return next(events_dir_at(root).glob("*.jsonl"), None) is not None
    except OSError:  # pragma: no cover - unreadable directory
        logger.debug("could not list %s", events_dir_at(root), exc_info=True)
        return False


def tasks_dir() -> Path:
    """Return ``<workspace>/session-tasks``, creating its subdirectories."""

    base = workspace_root() / TASKS_DIRNAME
    for child in (EVENTS_DIRNAME, INDEX_DIRNAME, DETAIL_DIRNAME, LOCK_DIRNAME):
        (base / child).mkdir(parents=True, exist_ok=True)
    return base


def events_dir() -> Path:
    return tasks_dir() / EVENTS_DIRNAME


def index_dir() -> Path:
    return tasks_dir() / INDEX_DIRNAME


def detail_dir() -> Path:
    return tasks_dir() / DETAIL_DIRNAME


def _lock_dir() -> Path:
    return tasks_dir() / LOCK_DIRNAME


def task_path(task_id: str) -> Path:
    """Return the JSONL path for ``task_id`` without touching the filesystem."""

    return tasks_dir() / EVENTS_DIRNAME / f"{_validate_task_id(task_id)}.jsonl"


def _is_reserved_device_name(task_id: str) -> bool:
    """True when ``task_id`` resolves to a Windows device name.

    ``nul``, ``NUL.txt``, ``com1`` and friends are reserved by the Win32
    namespace: ``nul.jsonl`` silently swallows writes and ``nul.lock`` can
    never be unlinked.
    """

    base = task_id.split(".", 1)[0].rstrip(" .").upper()
    return base in _RESERVED_DEVICE_NAMES


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a non-empty string")
    if task_id in (".", "..") or not set(task_id) <= _TASK_ID_SAFE:
        raise ValueError(f"unsafe task_id: {task_id!r}")
    if _is_reserved_device_name(task_id):
        raise ValueError(f"task_id is a reserved Windows device name: {task_id!r}")
    return task_id


#: Public spellings of the validator: callers that only have an id (the HTTP
#: detail route) must reject an unsafe one *before* deciding which workspace
#: to probe, otherwise ``nul`` turns into a 404 instead of a 400.
validate_task_id = _validate_task_id


# --------------------------------------------------------------------------- #
# Ids and timestamps
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    """Current time as ISO8601 with a timezone offset (local time)."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_task_id() -> str:
    """Readable, sortable, collision-free id: ``t-YYYYMMDD-HHMM-<8hex>``.

    The id is also the file name, so two tasks sharing an id would share an
    event log. 4 hex digits collide about 27% of the time for 30 batches of
    200 ids; 8 hex digits make that ~0. The process-local set below removes
    the residual birthday-collision risk inside one interpreter as well.
    """

    global _id_stamp

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    with _ID_GUARD:
        if stamp != _id_stamp:
            _id_stamp = stamp
            _issued_ids.clear()
        for _ in range(64):
            candidate = f"t-{stamp}-{secrets.token_hex(4)}"
            if candidate not in _issued_ids:
                _issued_ids.add(candidate)
                return candidate

    # 64 consecutive collisions (impossible in practice) - never loop forever.
    return f"t-{stamp}-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #
def _is_transient_lock_error(exc: OSError) -> bool:
    """True when ``exc`` means "the lock is unavailable right now, retry"."""

    if isinstance(exc, (FileExistsError, PermissionError, BlockingIOError)):
        return True
    if exc.errno in _TRANSIENT_LOCK_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def _sleep_before_retry() -> None:
    """Back off a little, with jitter so waiters do not move in lockstep."""

    time.sleep(LOCK_SLEEP_SECONDS + random.uniform(0.0, LOCK_SLEEP_JITTER_SECONDS))


def _acquire_lock(task_id: str) -> Path:
    """Grab an exclusive lock file for ``task_id``.

    Uses ``O_CREAT | O_EXCL`` (atomic on POSIX and Windows) and retries for
    ~4-6 s. Retries cover both "already held" (``FileExistsError``) and the
    Windows transient failures (``EACCES``/``EPERM``/``EBUSY`` and the
    sharing-violation ``winerror`` codes) that happen while another writer is
    unlinking the lock: treating only ``FileExistsError`` as retryable drops
    events under concurrency.
    """

    lock_path = _lock_dir() / f"{_validate_task_id(task_id)}.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    last_error: Optional[OSError] = None

    for _ in range(LOCK_RETRIES):
        try:
            fd = os.open(str(lock_path), flags, 0o644)
        except OSError as exc:
            if not _is_transient_lock_error(exc):
                raise
            last_error = exc
            if isinstance(exc, FileExistsError) and _is_stale(lock_path):
                logger.warning("removing stale lock %s", lock_path)
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass
                continue
            _sleep_before_retry()
            continue

        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:  # pragma: no cover - lock content is only diagnostic
            logger.debug("could not write lock owner to %s", lock_path, exc_info=True)
        finally:
            os.close(fd)
        return lock_path

    raise TimeoutError(
        f"could not acquire event store lock for {task_id!r} "
        f"after {LOCK_RETRIES} attempts"
    ) from last_error


def _is_stale(lock_path: Path, stale_after: float = LOCK_STALE_SECONDS) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age > stale_after


def _release_lock(lock_path: Path) -> None:
    """Remove the lock, retrying a transient sharing violation briefly.

    A lock file left behind blocks every later writer until it goes stale, so
    a single ignored ``EACCES`` here turns into a 30 s outage for that task.
    """

    for attempt in range(LOCK_RELEASE_RETRIES):
        try:
            os.unlink(str(lock_path))
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            transient = _is_transient_lock_error(exc)
            if not transient or attempt == LOCK_RELEASE_RETRIES - 1:
                logger.warning("could not remove lock %s", lock_path, exc_info=True)
                return
            time.sleep(LOCK_RELEASE_SLEEP_SECONDS)


# --------------------------------------------------------------------------- #
# Read / write
# --------------------------------------------------------------------------- #
def _reject_json_constant(token: str) -> None:
    """``parse_constant`` hook: refuse ``NaN`` / ``Infinity`` / ``-Infinity``.

    They are not JSON; Python accepts them by default and a record such as
    ``{"x":NaN}`` would then enter the event list without a ``seq`` and
    inflate the next ``seq``.

    Only the *literal* spellings reach this hook. A number that overflows
    while parsing (``1e309``) is legal JSON and yields ``inf`` silently, which
    is why :func:`_has_non_finite` exists.
    """

    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def _has_non_finite(value: object) -> bool:
    """True when ``value`` holds a non-finite float anywhere inside it.

    Covers the overflow case the parser accepts without a hook:
    ``json.loads('{"n": 1e309}')`` returns ``float('inf')``. Walks dicts and
    lists iteratively so deep nesting cannot hit the recursion limit.

    ``bool`` is an ``int``, never a ``float``, so ``true``/``false`` pass.
    """

    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                return True
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return False


def read_events(task_id: str) -> list[dict]:
    """Return all readable events for ``task_id`` in file order.

    Missing files yield an empty list. Corrupt lines are logged and skipped so
    a single bad append can never make the log unreadable.
    """

    path = task_path(task_id)
    if not path.exists():
        return []

    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for lineno, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line, parse_constant=_reject_json_constant)
                except ValueError as exc:  # includes json.JSONDecodeError
                    logger.warning(
                        "skipping corrupt event line %s:%d (%s)", path, lineno, exc
                    )
                    continue
                if not isinstance(record, dict):
                    logger.warning(
                        "skipping non-object event line %s:%d (%s)",
                        path,
                        lineno,
                        type(record).__name__,
                    )
                    continue
                if _has_non_finite(record):
                    # ``1e309`` is valid JSON; json.loads overflows it to inf
                    # without calling parse_constant. Writing such a record
                    # would also be impossible (allow_nan=False), so skip it.
                    logger.warning(
                        "skipping event line %s:%d (non-finite number)",
                        path,
                        lineno,
                    )
                    continue
                events.append(record)
    except OSError:  # pragma: no cover - e.g. file deleted mid-read
        logger.warning("could not read event log %s", path, exc_info=True)
        return events

    return events


def _next_seq(events: list[dict]) -> int:
    """One past the highest valid ``seq`` on disk.

    Deliberately *not* ``len(events) + 1``: a skipped line (corrupt,
    non-finite, or a legacy record without ``seq``) would otherwise consume a
    number and leave a hole (``[1, 2, 4]``) or make ``seq`` depend on how many
    records happen to be readable. Any entry whose ``seq`` is missing or not
    an ``int`` is ignored, so appends stay contiguous.
    """

    highest = 0
    for record in events:
        seq = record.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > highest:
            highest = seq
    return highest + 1


def _has_torn_tail(path: Path) -> bool:
    """True when the file is non-empty and does not end with a newline."""

    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:  # pragma: no cover - unreadable file: just append
        logger.warning("could not inspect tail of %s", path, exc_info=True)
        return False


def _append_line(path: Path, payload: bytes) -> None:
    """Append ``payload`` (one JSONL line) and heal a torn trailing line.

    Without the extra ``b"\\n"`` the new record is glued onto the half-written
    line: the reader then sees one corrupt line (the new event is invisible
    and the next ``seq`` repeats).
    """

    with open(path, "ab") as handle:
        if _has_torn_tail(path):
            logger.warning("healing torn tail of %s with a newline", path)
            handle.write(b"\n")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def append_event(task_id: str, event: dict) -> dict:
    """Append ``event`` to the task log and return the stored record.

    ``seq`` and ``at`` are filled in by the store (callers may not inject
    ``seq``). The whole read-seq-append cycle happens while holding the task
    lock, so concurrent writers get a gap-free, strictly increasing sequence.
    """

    if not isinstance(event, dict):
        raise TypeError("event must be a dict")

    task_id = _validate_task_id(task_id)
    path = task_path(task_id)

    lock_path = _acquire_lock(task_id)
    try:
        record = dict(event)
        record.pop("seq", None)
        record["seq"] = _next_seq(read_events(task_id))
        record.setdefault("at", _now_iso())

        try:
            line = json.dumps(
                record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
        except ValueError as exc:  # NaN/Infinity in the payload
            raise ValueError(
                f"event contains a value JSON cannot represent: {exc}"
            ) from exc
        payload = line.encode("utf-8") + b"\n"

        _append_line(path, payload)
    finally:
        _release_lock(lock_path)

    return record
