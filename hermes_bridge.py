"""Bridge between the HAL web frontend and Hermes Agent.

Two implementations, selected by HAL_BRIDGE (default "acp"):

acp        One persistent `hermes-acp` process speaking the Agent Client
           Protocol over stdio (agent-client-protocol library). No per-turn
           CLI startup cost — a turn is just session/prompt. ACP sessions
           persist to ~/.hermes/state.db, so browser sessions survive both
           bridge and agent restarts via session/load.

subprocess One `hermes chat -Q -q` process per turn (the original bridge).
           Contract: stdout = clean reply, stderr = `session_id: <id>`.

Both persist the cookie-session -> hermes-session map in DATA_DIR and
serialize turns per browser session — Hermes sessions are single-writer.
The HAL persona comes from AGENTS.md in the agent cwd in both modes.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import socket
import threading
import time
import uuid
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

def _default_hermes_executable(name: str) -> str:
    """Find Hermes across the current installer and the legacy checkout."""
    candidates = (
        Path("~/.hermes/hermes-agent/venv/bin").expanduser() / name,
        Path("~/hermes-agent/.venv/bin").expanduser() / name,
        # Upstream's setup-hermes.sh creates "venv", not ".venv", on its Termux
        # path — so on the phone hermes-acp exists and was invisible here.
        Path("~/hermes-agent/venv/bin").expanduser() / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or str(candidates[0])


BRIDGE_MODE = os.environ.get("HAL_BRIDGE", "acp").strip().lower()
HERMES_BIN = os.path.expanduser(
    os.environ.get("HAL_HERMES_BIN", _default_hermes_executable("hermes"))
)
HERMES_ACP_BIN = os.path.expanduser(
    os.environ.get("HAL_HERMES_ACP_BIN", _default_hermes_executable("hermes-acp"))
)
AGENT_CWD = os.path.expanduser(os.environ.get("HAL_AGENT_CWD", str(Path(__file__).resolve().parent)))
AGENT_TIMEOUT = float(os.environ.get("HAL_AGENT_TIMEOUT", "180"))
OFFLINE_PREFLIGHT = os.environ.get("HAL_OFFLINE_PREFLIGHT", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
OFFLINE_CHECK_HOSTS = os.environ.get(
    "HAL_OFFLINE_CHECK_HOSTS",
    "1.1.1.1:443,api.openai.com:443,openrouter.ai:443",
)
OFFLINE_CHECK_TIMEOUT = float(os.environ.get("HAL_OFFLINE_CHECK_TIMEOUT", "0.4"))
OFFLINE_CHECK_TTL = float(os.environ.get("HAL_OFFLINE_CHECK_TTL", "30"))
# Tool-permission handling (ACP mode): "deny" rejects every request (the
# safe default), "ask" surfaces Allow/Deny to the browser — and to a spoken
# yes/no — then waits, "yolo" auto-approves everything. HAL_YOLO=1 remains a
# back-compat alias for yolo; the subprocess-mode equivalent of yolo is
# HAL_HERMES_ARGS="--yolo".
_mode = os.environ.get("HAL_PERMISSION_MODE", "").strip().lower()
if not _mode:
    _mode = "yolo" if os.environ.get("HAL_YOLO", "") == "1" else "deny"
PERMISSION_MODE = _mode if _mode in {"deny", "ask", "yolo"} else "deny"
# Seconds an "ask" waits for a decision before it is denied.
PERMISSION_TIMEOUT = float(os.environ.get("HAL_PERMISSION_TIMEOUT", "30"))
YOLO = PERMISSION_MODE == "yolo"
SESSION_SOURCE = "hal-web"
# Extra CLI args for subprocess mode, e.g. HAL_HERMES_ARGS="-m gpt-5.4 --yolo"
EXTRA_ARGS = shlex.split(os.environ.get("HAL_HERMES_ARGS", ""))

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)", re.M)


class KeyedLocks:
    """One asyncio.Lock per key, evicted only when truly idle.

    Eviction can't just test lock.locked(): between release() and a queued
    waiter re-acquiring, locked() is False, so a lock with waiters would be
    dropped and the next turn would mint a fresh one — two turns running
    concurrently on a single-writer Hermes session. Refcount holders and
    waiters instead."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        self._refs[key] = self._refs.get(key, 0) + 1
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            if self._refs[key] == 1:
                del self._refs[key]
                self._locks.pop(key, None)
            else:
                self._refs[key] -= 1


_cookie_locks = KeyedLocks()

FAILURE_LINE = "I'm sorry, Dave. I'm afraid something went wrong on my end."
TIMEOUT_LINE = "I'm sorry, Dave. That took longer than I allow myself. Please try again."
OFFLINE_LINE = "I'm sorry, Dave. I am disconnected from inference right now."

_network_lock = threading.Lock()
_network_check_at = 0.0
_network_check_online: bool | None = None


def _parse_check_hosts(raw: str) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep:
            host, port = item, "443"
        try:
            hosts.append((host.strip("[]"), int(port)))
        except ValueError:
            continue
    return hosts


def _network_available() -> bool:
    """Fast, cached internet check before sending a turn to remote inference."""
    global _network_check_at, _network_check_online
    if not OFFLINE_PREFLIGHT:
        return True

    with _network_lock:
        now = time.monotonic()
        if _network_check_online is not None and now - _network_check_at < OFFLINE_CHECK_TTL:
            return _network_check_online

    hosts = _parse_check_hosts(OFFLINE_CHECK_HOSTS)
    if not hosts:
        return True

    def can_connect(target: tuple[str, int]) -> bool:
        host, port = target
        try:
            with socket.create_connection((host, port), timeout=OFFLINE_CHECK_TIMEOUT):
                return True
        except OSError:
            return False

    online = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = [pool.submit(can_connect, target) for target in hosts]
        try:
            for future in concurrent.futures.as_completed(futures, timeout=OFFLINE_CHECK_TIMEOUT):
                if future.result():
                    online = True
                    break
        except concurrent.futures.TimeoutError:
            online = False

    with _network_lock:
        _network_check_at = time.monotonic()
        _network_check_online = online
    return online


class SessionMap:
    """Persistent cookie-session -> hermes-session-id mapping."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        try:
            self._map: dict[str, str] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            self._map = {}

    def get(self, session_id: str) -> str | None:
        return self._map.get(session_id)

    def set(self, session_id: str, hermes_id: str) -> None:
        with self._lock:
            self._map[session_id] = hermes_id
            self._flush()

    def drop(self, session_id: str) -> None:
        with self._lock:
            if self._map.pop(session_id, None) is not None:
                self._flush()

    def _flush(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._map, indent=1))
        tmp.replace(self._path)


_session_map: SessionMap | None = None
_data_dir: Path | None = None


def init(data_dir: Path) -> None:
    global _session_map, _data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    _data_dir = data_dir
    _session_map = SessionMap(data_dir / "hermes_sessions.json")


def acp_session_for(cookie_id: str) -> str | None:
    return _session_map.get(cookie_id) if _session_map is not None else None


# ---------------------------------------------------------------------------
# In-process SSE event fan-out — one asyncio.Queue per browser/cookie session.
# Transient (not persisted): tool-call/permission events are ephemeral UI
# signal, not conversation state.
# ---------------------------------------------------------------------------

_event_queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
_acp_to_cookie: dict[str, str] = {}  # acp session_id -> cookie_id


def register_event_queue(cookie_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _event_queues[cookie_id].add(q)
    return q


def unregister_event_queue(cookie_id: str, q: asyncio.Queue) -> None:
    queues = _event_queues.get(cookie_id)
    if queues is not None:
        queues.discard(q)
        if not queues:
            _event_queues.pop(cookie_id, None)


# Aliases let a synthetic session key (a mission's private session) deliver
# its events to the owning browser session's queues.
_publish_aliases: dict[str, str] = {}


def alias_events(alias_id: str, cookie_id: str) -> None:
    """Route events published under alias_id to cookie_id's SSE queues."""
    _publish_aliases[alias_id] = cookie_id


def unalias_events(alias_id: str) -> None:
    _publish_aliases.pop(alias_id, None)


# Optional observer for every published event, set by main.py (persistence,
# spoken permission prompts). Called on the event loop; must not block.
on_event = None  # Callable[[str, dict], None] | None

# Commentary sinks: session key -> callable(chunk_text). Registered by the
# transport around one turn's ask() so agent_message_chunk text can be
# spoken while the turn is still running. Called on the event loop per
# chunk; sinks must not block.
_commentary_sinks: dict = {}


def set_commentary_sink(cookie_id: str, sink) -> None:
    _commentary_sinks[cookie_id] = sink


def clear_commentary_sink(cookie_id: str, sink=None) -> None:
    """Remove the sink — identity-checked, like the active_websockets
    cleanup: a barged-in turn's finally must not evict the newer turn's
    sink that replaced it."""
    if sink is None or _commentary_sinks.get(cookie_id) is sink:
        _commentary_sinks.pop(cookie_id, None)


def publish_event_all(payload: dict) -> None:
    """Emit an SSE event to every connected browser session — for global
    surfaces (the viewscreen) that belong to no one session. Not journaled:
    broadcasts are ephemeral UI signal."""
    data = json.dumps(payload)
    for queues in list(_event_queues.values()):
        for q in list(queues):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


def publish_event(cookie_id: str, payload: dict) -> None:
    """Emit an SSE event for a browser session (aliases resolved). Events
    published under a mission's alias are tagged with the originating
    session so the UI can attribute tool calls to their mission."""
    owner = _publish_aliases.get(cookie_id)
    if owner is not None:
        payload = {**payload, "mission_session": cookie_id}
        cookie_id = owner
    if on_event is not None:
        try:
            on_event(cookie_id, payload)
        except Exception as exc:
            print(f"[hermes_bridge] event observer failed: {exc!r}")
    data = json.dumps(payload)
    for q in list(_event_queues.get(cookie_id, ())):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass  # ticker/eye state is ephemeral — drop rather than block


# Session keys whose tool-permission requests are auto-allowed regardless of
# PERMISSION_MODE (ACP mode only — subprocess mode has no permission
# callback). Granted per mission by a trigger's "permissions": "allow";
# data/triggers.json is the trust boundary. Keys are the synthetic
# cookie-session ids missions run under, never a browser session.
_tool_allowed_cookies: set[str] = set()


def allow_tools_for(cookie_id: str) -> None:
    _tool_allowed_cookies.add(cookie_id)


def disallow_tools_for(cookie_id: str) -> None:
    _tool_allowed_cookies.discard(cookie_id)


# ---------------------------------------------------------------------------
# Pending permission requests (PERMISSION_MODE == "ask"). All access happens
# on the event loop: request_permission awaits the future there, and the
# resolvers are async endpoints / coroutine code — no cross-thread set_result.
# ---------------------------------------------------------------------------

_pending_permissions: dict[str, tuple[asyncio.Future, str, str]] = {}  # id -> (fut, owner, title)


def _register_permission(owner_cookie: str, title: str) -> tuple[str, asyncio.Future]:
    request_id = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    _pending_permissions[request_id] = (fut, owner_cookie, title)
    return request_id, fut


def pending_permission_for(owner_cookie: str) -> str | None:
    """Oldest pending request id owned by this browser session, if any."""
    for request_id, (_fut, owner, _title) in _pending_permissions.items():
        if owner == owner_cookie:
            return request_id
    return None


def resolve_permission(request_id: str, allow: bool, owner_cookie: str) -> bool:
    """Answer a pending request. Ownership is checked so one browser session
    cannot approve another session's tools. Returns False if unknown/foreign."""
    entry = _pending_permissions.get(request_id)
    if entry is None:
        return False
    fut, owner, _title = entry
    if owner != owner_cookie or fut.done():
        return False
    fut.set_result(allow)
    return True


# ---------------------------------------------------------------------------
# ACP mode — one persistent hermes-acp process
# ---------------------------------------------------------------------------


class _TurnAborted(Exception):
    """The agent refused the turn (e.g. its session state evaporated)."""


def _load_acp():
    from acp import PROTOCOL_VERSION, spawn_agent_process  # noqa: F401
    from acp.schema import (  # noqa: F401
        AllowedOutcome,
        ClientCapabilities,
        DeniedOutcome,
        RequestPermissionResponse,
        TextContentBlock,
    )
    return locals()


class _HALClient:
    """Client half of the ACP connection: collects reply text, answers
    permission requests. Chunks are only recorded while a turn is active for
    that session, so session/load history replay never leaks into a reply."""

    def __init__(self, acp_mod):
        self._acp = acp_mod
        self._buffers: dict[str, list[str]] = {}
        self._active: set[str] = set()
        self._commands: dict[str, list[dict[str, str | None]]] = {}
        self._command_events: dict[str, asyncio.Event] = {}

    def begin(self, session_id: str) -> None:
        self._buffers[session_id] = []
        self._active.add(session_id)

    def finish(self, session_id: str) -> str:
        self._active.discard(session_id)
        return "".join(self._buffers.pop(session_id, [])).strip()

    def commands(self, session_id: str) -> list[dict[str, str | None]]:
        return [dict(command) for command in self._commands.get(session_id, [])]

    async def wait_for_commands(
        self, session_id: str, timeout: float = 1.5
    ) -> list[dict[str, str | None]]:
        commands = self.commands(session_id)
        if commands:
            return commands
        event = self._command_events.setdefault(session_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return []
        return self.commands(session_id)

    def forget(self, session_id: str) -> None:
        self._commands.pop(session_id, None)
        self._command_events.pop(session_id, None)

    async def session_update(self, session_id: str, update, **kwargs) -> None:
        kind = getattr(update, "session_update", "")
        if kind == "available_commands_update":
            commands: list[dict[str, str | None]] = []
            for command in getattr(update, "available_commands", None) or []:
                name = getattr(command, "name", None)
                if not name and isinstance(command, dict):
                    name = command.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                description = getattr(command, "description", None)
                if description is None and isinstance(command, dict):
                    description = command.get("description")
                input_spec = getattr(command, "input", None)
                if input_spec is None and isinstance(command, dict):
                    input_spec = command.get("input")
                input_value = getattr(input_spec, "root", input_spec)
                if isinstance(input_spec, dict) and "root" in input_spec:
                    input_value = input_spec["root"]
                hint = getattr(input_value, "hint", None)
                if hint is None and isinstance(input_value, dict):
                    hint = input_value.get("hint")
                commands.append({
                    "name": name.strip().lstrip("/"),
                    "description": str(description or "").strip(),
                    "input_hint": str(hint).strip() if hint else None,
                    "source": "hermes",
                })
            self._commands[session_id] = commands
            self._command_events.setdefault(session_id, asyncio.Event()).set()
            cookie_id = _acp_to_cookie.get(session_id)
            if cookie_id is not None:
                publish_event(cookie_id, {
                    "type": "commands_update",
                    "commands": commands,
                })
        elif kind == "agent_message_chunk":
            if session_id not in self._active:
                return
            text = getattr(getattr(update, "content", None), "text", "") or ""
            if text:
                self._buffers.setdefault(session_id, []).append(text)
                cookie_id = _acp_to_cookie.get(session_id)
                sink = _commentary_sinks.get(cookie_id) if cookie_id else None
                if sink is not None:
                    try:
                        sink(text)
                    except Exception as exc:
                        print(f"[hermes_bridge] commentary sink failed: {exc!r}")
        elif kind in ("tool_call", "tool_call_update"):
            cookie_id = _acp_to_cookie.get(session_id)
            if cookie_id is None:
                return  # session/load replay before any turn bound this session
            publish_event(cookie_id, {
                "type": kind,
                "tool_call_id": getattr(update, "tool_call_id", None),
                "title": getattr(update, "title", None),
                "kind": getattr(update, "kind", None),
                "status": getattr(update, "status", None),
            })

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        m = self._acp
        title = getattr(tool_call, "title", None) or "a tool"
        tool_call_id = getattr(tool_call, "tool_call_id", None)
        cookie_id = _acp_to_cookie.get(session_id)
        allow_opt = next((o for o in options if o.kind == "allow_once"), None) or next(
            (o for o in options if o.kind == "allow_always"), None
        )

        def allowed_response():
            return m["RequestPermissionResponse"](
                outcome=m["AllowedOutcome"](outcome="selected", option_id=allow_opt.option_id)
            )

        if cookie_id in _tool_allowed_cookies and allow_opt is not None:
            print(f"[hermes_bridge] auto-allowing tool call (trigger permissions): {title}")
            return allowed_response()

        if PERMISSION_MODE == "yolo" and allow_opt is not None:
            print(f"[hermes_bridge] auto-allowing tool call (permission mode yolo): {allow_opt.name}")
            return allowed_response()

        if PERMISSION_MODE == "ask" and allow_opt is not None and cookie_id is not None:
            # A mission's events alias to the owning browser session; the
            # pending entry must carry that owner or the browser can't answer.
            owner = _publish_aliases.get(cookie_id, cookie_id)
            request_id, fut = _register_permission(owner, title)
            publish_event(cookie_id, {
                "type": "permission_request",
                "request_id": request_id,
                "tool_call_id": tool_call_id,
                "title": title,
                "timeout": PERMISSION_TIMEOUT,
            })
            try:
                allow = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT)
            except asyncio.TimeoutError:
                allow = False
            finally:
                _pending_permissions.pop(request_id, None)
            publish_event(cookie_id, {
                "type": "permission_resolved",
                "request_id": request_id,
                "title": title,
                "allowed": allow,
            })
            if allow:
                print(f"[hermes_bridge] Dave allowed: {title}")
                return allowed_response()
            print(f"[hermes_bridge] permission not granted (denied or timed out): {title}")
            return m["RequestPermissionResponse"](outcome=m["DeniedOutcome"](outcome="cancelled"))

        print("[hermes_bridge] denying tool permission request (HAL_PERMISSION_MODE=deny)")
        if cookie_id:
            publish_event(cookie_id, {
                "type": "permission_denied",
                "tool_call_id": tool_call_id,
                "title": title,
            })
        return m["RequestPermissionResponse"](outcome=m["DeniedOutcome"](outcome="cancelled"))

    # We advertise no fs/terminal capabilities, so these should never fire.
    async def write_text_file(self, **kwargs):
        return None

    async def read_text_file(self, **kwargs):
        raise RuntimeError("fs capability not advertised")

    async def create_terminal(self, **kwargs):
        raise RuntimeError("terminal capability not advertised")

    async def terminal_output(self, **kwargs):
        raise RuntimeError("terminal capability not advertised")

    async def release_terminal(self, **kwargs):
        return None

    async def wait_for_terminal_exit(self, **kwargs):
        raise RuntimeError("terminal capability not advertised")

    async def kill_terminal(self, **kwargs):
        return None

    async def ext_method(self, method: str, params: dict) -> dict:
        return {}

    async def ext_notification(self, method: str, params: dict) -> None:
        return None

    def on_connect(self, conn) -> None:
        return None


class ACPBridge:
    def __init__(self):
        self._stack: AsyncExitStack | None = None
        self._conn = None
        self._proc = None
        self._client: _HALClient | None = None
        self._loaded: set[str] = set()
        self._restart_lock = asyncio.Lock()
        self._acp = None

    async def start(self) -> None:
        async with self._restart_lock:
            await self._ensure_started_locked()

    async def stop(self) -> None:
        async with self._restart_lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:
                print(f"[hermes_bridge] ACP teardown: {exc}")
        self._stack = None
        self._conn = None
        self._proc = None
        self._client = None
        self._loaded = set()

    def _alive(self) -> bool:
        return self._conn is not None and self._proc is not None and self._proc.returncode is None

    def health(self) -> dict:
        alive = self._alive()
        return {"alive": alive, "pid": self._proc.pid if alive else None}

    def forget(self, session_id: str) -> None:
        self._loaded.discard(session_id)
        if self._client is not None:
            self._client.forget(session_id)

    async def _ensure_started_locked(self) -> None:
        if self._alive():
            return
        await self._teardown_locked()
        if self._acp is None:
            self._acp = _load_acp()
        m = self._acp
        log_path = (_data_dir or Path(".")) / "acp.log"
        try:
            # Append-mode forever otherwise; one rotation generation is enough.
            if log_path.stat().st_size > 5 * 1024 * 1024:
                log_path.replace(log_path.with_suffix(".log.1"))
        except OSError:
            pass
        log_file = open(log_path, "ab", buffering=0)
        self._client = _HALClient(m)
        self._stack = AsyncExitStack()
        self._stack.callback(log_file.close)
        env = {**os.environ, "HERMES_ACCEPT_HOOKS": "1"}
        conn, proc = await self._stack.enter_async_context(
            m["spawn_agent_process"](
                self._client,
                HERMES_ACP_BIN,
                cwd=AGENT_CWD,
                env=env,
                transport_kwargs={"stderr": log_file},
            )
        )
        await asyncio.wait_for(
            conn.initialize(
                protocol_version=m["PROTOCOL_VERSION"],
                client_capabilities=m["ClientCapabilities"](),
            ),
            timeout=60,
        )
        self._conn, self._proc = conn, proc
        print(f"[hermes_bridge] hermes-acp up (pid={proc.pid}, log={log_path})")

    async def _resolve_session(self, cookie_id: str) -> str:
        assert _session_map is not None
        if not self._alive():
            raise RuntimeError("ACP bridge is down during session resolution")
        acp_id = _session_map.get(cookie_id)
        if acp_id and acp_id in self._loaded:
            return acp_id
        if acp_id:
            try:
                resp = await asyncio.wait_for(
                    self._conn.load_session(cwd=AGENT_CWD, session_id=acp_id), timeout=60
                )
                if resp is not None:
                    self._loaded.add(acp_id)
                    return acp_id
            except Exception as exc:
                print(f"[hermes_bridge] session/load {acp_id} failed ({exc}); starting fresh")
        resp = await asyncio.wait_for(
            self._conn.new_session(cwd=AGENT_CWD, mcp_servers=[]), timeout=60
        )
        _session_map.set(cookie_id, resp.session_id)
        self._loaded.add(resp.session_id)
        return resp.session_id

    async def ask(self, text: str, cookie_id: str) -> str:
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                async with self._restart_lock:
                    await self._ensure_started_locked()
                session_id = await self._resolve_session(cookie_id)
                _acp_to_cookie[session_id] = cookie_id
                return await self._prompt(session_id, cookie_id, text)
            except asyncio.TimeoutError:
                return TIMEOUT_LINE
            except _TurnAborted:
                # Agent-side session state is gone; forget it and retry fresh.
                _session_map.drop(cookie_id)
                last_exc = None
                continue
            except Exception as exc:
                print(f"[hermes_bridge] ACP attempt {attempt} failed: {exc!r}")
                last_exc = exc
                await self.stop()
        if last_exc is not None:
            print(f"[hermes_bridge] giving up on ACP turn: {last_exc!r}")
        return FAILURE_LINE

    async def cancel(self, cookie_id: str) -> None:
        """Interrupt the in-flight prompt on a session (mission cancel).
        Best-effort: an unmapped or unstarted session is a no-op."""
        acp_id = _session_map.get(cookie_id) if _session_map is not None else None
        if acp_id and self._alive():
            try:
                await self._conn.cancel(session_id=acp_id)
            except Exception as exc:
                print(f"[hermes_bridge] cancel {acp_id} failed: {exc!r}")

    async def commands(self, cookie_id: str) -> list[dict[str, str | None]]:
        """Resolve the browser's ACP session and return its advertised slash
        commands. Hermes owns this catalog; HAL only transports it."""
        async with self._restart_lock:
            await self._ensure_started_locked()
        session_id = await self._resolve_session(cookie_id)
        _acp_to_cookie[session_id] = cookie_id
        assert self._client is not None
        commands = self._client.commands(session_id)
        if commands:
            return commands
        return await self._client.wait_for_commands(session_id)

    async def _prompt(self, session_id: str, cookie_id: str, text: str) -> str:
        m = self._acp
        assert self._client is not None
        self._client.begin(session_id)
        try:
            resp = await asyncio.wait_for(
                self._conn.prompt(
                    prompt=[m["TextContentBlock"](type="text", text=text)],
                    session_id=session_id,
                ),
                timeout=AGENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._client.finish(session_id)
            try:
                await self._conn.cancel(session_id=session_id)
            except Exception:
                pass
            raise
        reply = self._client.finish(session_id)
        stop_reason = getattr(resp, "stop_reason", "end_turn")
        if not reply and stop_reason == "refusal":
            self._loaded.discard(session_id)
            raise _TurnAborted(f"prompt refused for {session_id}")
        return reply or FAILURE_LINE


_acp_bridge = ACPBridge() if BRIDGE_MODE == "acp" else None


# ---------------------------------------------------------------------------
# Subprocess mode — one `hermes chat -Q -q` per turn (fallback)
# ---------------------------------------------------------------------------


async def _ask_subprocess(text: str, session_id: str) -> str:
    assert _session_map is not None
    cmd = [HERMES_BIN, "chat", "-Q", "-q", "--source", SESSION_SOURCE, *EXTRA_ARGS]
    hermes_id = _session_map.get(session_id)
    if hermes_id:
        cmd += ["--resume", hermes_id]
    cmd += ["--", text]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=AGENT_CWD,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[hermes_bridge] timeout after {AGENT_TIMEOUT}s (session={session_id})")
        return TIMEOUT_LINE

    stderr_text = _ANSI_RE.sub("", err.decode("utf-8", "replace"))
    match = _SESSION_ID_RE.search(stderr_text)
    if match and match.group(1) != hermes_id:
        _session_map.set(session_id, match.group(1))

    reply = _ANSI_RE.sub("", out.decode("utf-8", "replace")).strip()
    if proc.returncode != 0 or not reply:
        print(
            f"[hermes_bridge] hermes exit={proc.returncode} (session={session_id}) "
            f"stderr: {stderr_text.strip()[-2000:]}"
        )
        return reply or FAILURE_LINE
    return reply


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def startup() -> None:
    """Warm the persistent agent so the first spoken turn isn't slow."""
    if _acp_bridge is not None:
        try:
            await _acp_bridge.start()
        except Exception as exc:
            # Lazy retry happens on the first ask(); don't block server boot.
            print(f"[hermes_bridge] ACP warmup failed (will retry on first turn): {exc!r}")


async def shutdown() -> None:
    if _acp_bridge is not None:
        await _acp_bridge.stop()


def bridge_health() -> dict:
    """Liveness of the thinking half, for /api/health and /api/status."""
    info: dict = {"mode": BRIDGE_MODE}
    if _acp_bridge is None:
        # Subprocess mode has no persistent process to probe.
        info.update(alive=True, pid=None)
    else:
        info.update(_acp_bridge.health())
    return info


async def cancel_session(cookie_id: str) -> None:
    """Interrupt the turn running on this session key, if any (ACP mode only
    — subprocess mode has no handle on its per-turn process)."""
    if _acp_bridge is not None:
        await _acp_bridge.cancel(cookie_id)


async def list_slash_commands(cookie_id: str) -> list[dict[str, str | None]]:
    """Return the live slash-command catalog advertised by Hermes ACP."""
    if _acp_bridge is None:
        return []
    async with _cookie_locks.hold(cookie_id):
        return await _acp_bridge.commands(cookie_id)


def drop_session(cookie_id: str) -> None:
    """Forget the Hermes session behind a browser session (/api/session/reset).
    The agent-side session record in ~/.hermes/state.db is left orphaned."""
    if _session_map is None:
        return
    acp_id = _session_map.get(cookie_id)
    _session_map.drop(cookie_id)
    if acp_id:
        _acp_to_cookie.pop(acp_id, None)
        if _acp_bridge is not None:
            _acp_bridge.forget(acp_id)
    _event_queues.pop(cookie_id, None)


def _with_session_token_hint(text: str, session_id: str) -> str:
    """Prepend the real browser session id so the model has something correct
    to echo back as `session_token` on hal-robot MCP tool calls.

    Nothing else in this bridge ever tells the model that value — there is no
    ACP field or MCP resource carrying it across today. Without this hint the
    model must invent a session_token, and a permission_request published
    against an invented id reaches no browser's SSE stream: the Allow/Deny bar
    never appears, however long the arm request blocks (confirmed live,
    2026-09-06 — request_motion_authorization/crawl_arm calls that returned
    "working" and then nothing, because the event went nowhere anyone was
    watching). The hint is cheap and harmless on turns that never touch a
    robot tool, so it is sent unconditionally rather than gated on tool use.
    """
    return f"[session_token for any hal-robot tool call: {session_id}]\n\n{text}"


async def ask_hermes(text: str, session_id: str) -> str:
    """Send one utterance to Hermes and return its reply text."""
    assert _session_map is not None, "hermes_bridge.init() not called"
    if not await asyncio.to_thread(_network_available):
        print("[hermes_bridge] offline preflight blocked remote inference")
        return OFFLINE_LINE
    text = _with_session_token_hint(text, session_id)
    async with _cookie_locks.hold(session_id):
        if _acp_bridge is not None:
            result = await _acp_bridge.ask(text, session_id)
        else:
            result = await _ask_subprocess(text, session_id)
    return result
