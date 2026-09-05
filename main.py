"""Hermes Hal voice frontend for Hermes Agent CLI.

Fork of https://huggingface.co/spaces/piclez/hal rewired to run fully local:
  - STT:   faster-whisper (bundled with the Hermes venv) instead of Groq
  - Brain: Hermes Agent CLI (named sessions, full tool access) instead of Claude
  - TTS:   campwill/HAL-9000-Piper-TTS with Hermes Hal text normalization
           and optional ffmpeg mastering

Run with the Hermes venv:  ./run.sh   (or see README.md)
"""
import ast
import base64
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

import asyncio
from collections import deque

# Keep ONNX Runtime's telemetry identifier out of the repository and disable
# upstream usage reporting before Piper or faster-whisper imports ONNX.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

import numpy as np

from fastapi import FastAPI, File, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from piper import PiperVoice, SynthesisConfig
from pydantic import BaseModel

import chess_control
import chess_engine
import hermes_bridge
from hermes_bridge import ask_hermes
import ledger
import mission_control
import robot_tools
import speaker_id
import stopwords

APP_DIR = Path(__file__).resolve().parent
# The voice model ships in this repository. Preferring it over the Hermes venv's
# copy is what the isolation rule asks for — no provider-owned path searched
# implicitly — and it is also the only one that exists on the phone, where there
# is no ~/.hermes/voices at all. The Hermes copy is still used if it is there and
# the repo's is not, so an existing desktop install is unaffected.
_repo_voice = APP_DIR / "models" / "hal.onnx"
_hermes_voice = Path(os.path.expanduser("~/.hermes/voices/hal9000/hal9000.onnx"))
_default_voice = _repo_voice if _repo_voice.exists() else _hermes_voice
VOICE_PATH = Path(
    os.path.expanduser(os.environ.get("HAL_VOICE", str(_default_voice)))
)
STT_MODEL_NAME = os.environ.get("HAL_STT_MODEL", "base.en")
STT_BEAM_SIZE = int(os.environ.get("HAL_STT_BEAM", "5"))
# "auto" picks CUDA when a GPU is present and falls back to CPU otherwise —
# previously hardcoded to "cpu", which silently left GPU acceleration on the
# table. compute_type "auto" likewise picks the fastest type the resolved
# device supports (int8 on CPU, float16 on GPU) — matches the old explicit
# int8 default on CPU-only machines, so this is a no-op for them.
STT_DEVICE = os.environ.get("HAL_STT_DEVICE", "auto")
STT_COMPUTE_TYPE = os.environ.get("HAL_STT_COMPUTE_TYPE", "auto")
# 0 = let ctranslate2 pick (usually all physical cores); set explicitly on a
# shared/throttled box to stop STT from starving everything else.
STT_CPU_THREADS = int(os.environ.get("HAL_STT_CPU_THREADS", "0"))
# Whisper's fixed input rate — only used for the boot-time device probe.
SAMPLE_RATE_STT = 16000
# Skip loading the STT/TTS models — for tests of the pure-python parts only;
# /api/talk and /api/say will not work.
SKIP_MODELS = os.environ.get("HAL_SKIP_MODELS", "") == "1"
# Optional bias prompt for whisper, e.g. "Dave speaking with Hermes Hal." —
# helps it spell HAL/Hermes correctly. Off by default: a bias prompt can make
# whisper hallucinate text on near-silent recordings.
STT_PROMPT = os.environ.get("HAL_STT_PROMPT", "").strip() or None
# whisper.cpp is the Termux/Pixel STT backend (faster-whisper's ctranslate2
# has no usable Whisper bindings there — see termux_whisper_cpp.py).
# Auto-detected by presence rather than by a platform flag someone has to keep
# in sync by hand. Absent on the Mac unless someone separately builds it there
# too, so this changes nothing about the existing faster-whisper path.
WHISPER_CPP_BIN = os.path.expanduser(
    os.environ.get("HAL_WHISPER_CPP_BIN", "~/whisper.cpp/build/bin/whisper-cli")
)
WHISPER_CPP_MODEL = os.path.expanduser(
    os.environ.get("HAL_WHISPER_CPP_MODEL", f"~/whisper.cpp/models/ggml-{STT_MODEL_NAME}.bin")
)
# One setting for "where ffmpeg is" across this app, not two to keep in sync.
FFMPEG_BIN = os.environ.get("HAL_FFMPEG_BIN", "ffmpeg")
# On-device listen/speak loop for the Termux/Pixel deployment (termux_voice.py)
# — the phone's own mic/speaker, entirely separate from the browser-audio
# endpoints the desktop uses. See that module for why it can't share them.
TERMUX_LISTEN = os.environ.get("HAL_TERMUX_LISTEN", "0").strip().lower() not in {
    "0",
    "false",
    "no",
}
# History files hold messages, not turns — one spoken turn appends two.
MAX_HISTORY_MESSAGES = 40
MAX_SPOKEN_CHARS = 1500
# Transcripts travel in response headers; percent-encoding inflates ~3x and
# proxies/browsers cap header blocks, so bound them. Full text stays in history.
MAX_TRANSCRIPT_HEADER_CHARS = 2000
MAX_UPLOAD_BYTES = int(float(os.environ.get("HAL_MAX_UPLOAD_MB", "25")) * 1024 * 1024)
# Session cookie must outlive the browser process or the whole persistence
# story (ACP session/load, history files) dies with the window.
SESSION_COOKIE_MAX_AGE = int(float(os.environ.get("HAL_COOKIE_MAX_AGE_DAYS", "180")) * 86400)
SYSTEMS_CACHE_TTL = float(os.environ.get("HAL_SYSTEMS_TTL", "20"))
LATENCY_LOG = os.environ.get("HAL_LATENCY_LOG", "1").strip().lower() not in {"0", "false", "no"}
TTS_MASTERING = os.environ.get("HAL_TTS_MASTERING", "0").strip().lower() not in {
    "0",
    "false",
    "no",
}
MAX_CLI_OUTPUT_CHARS = int(os.environ.get("HAL_CLI_MAX_CHARS", "16000"))
HERMES_CLI_TIMEOUT = float(os.environ.get("HAL_CLI_TIMEOUT", "12"))
DATA_DIR = Path(os.environ.get("HAL_DATA_DIR", str(APP_DIR / "data")))
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
# The viewscreen: any image/HTML/PDF the agent writes here appears on the
# Bridge — one drop-folder retrofits visual output onto every toolset.
VIEWSCREEN_DIR = DATA_DIR / "viewscreen"
VIEWSCREEN_DIR.mkdir(parents=True, exist_ok=True)
VIEWSCREEN_POLL = float(os.environ.get("HAL_VIEWSCREEN_POLL", "2"))
_VIEWSCREEN_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".html", ".htm", ".pdf"}
hermes_bridge.init(DATA_DIR)
mission_control.init(DATA_DIR)
chess_control.init(DATA_DIR)
speaker_id.init(DATA_DIR)
ledger.init(DATA_DIR)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HAL_SLASH_COMMANDS = (
    {
        "name": "mission",
        "description": "Start a background mission",
        "input_hint": "mission title",
        "source": "hal",
    },
    {
        "name": "ask",
        "description": "Ask the latest completed mission a follow-up",
        "input_hint": "question",
        "source": "hal",
    },
    {
        "name": "chess",
        "description": "Start or control a chess game",
        "input_hint": "black | white | resign",
        "source": "hal",
    },
    {
        "name": "remember",
        "description": "Add an item to the care ledger",
        "input_hint": "item",
        "source": "hal",
    },
    {
        "name": "resign",
        "description": "Resign the active chess game",
        "input_hint": None,
        "source": "hal",
    },
)

# Reuse Hermes' HAL text normalization; ffmpeg mastering is optional for speed.
_HAL_TTS_SCRIPT = Path(
    os.path.expanduser(
        os.environ.get("HAL_TTS_SCRIPT", "~/.hermes/scripts/hal_piper_tts.py")
    )
)


def _load_hal_tts_module():
    spec = importlib.util.spec_from_file_location("hal_piper_tts", _HAL_TTS_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hal_tts = _load_hal_tts_module() if _HAL_TTS_SCRIPT.exists() else None

# Import the full Hermes slash-command registry so HAL's /api/commands catalog
# can expose every command the CLI supports — the ACP adapter only advertises
# ~9 of them.  cli_only and gateway_only commands are excluded: they need a
# terminal or a messaging platform that HAL's voice/web frontend doesn't have.
_HERMES_COMMANDS_PY = Path(
    os.path.expanduser(
        os.environ.get(
            "HAL_HERMES_COMMANDS",
            "~/.hermes/hermes-agent/hermes_cli/commands.py",
        )
    )
)


def _load_hermes_commands() -> tuple[dict[str, str | None], ...]:
    """Parse COMMAND_REGISTRY from Hermes via AST and return HAL-catalog dicts.

    This avoids importlib which fails on internal Hermes dependencies.
    """
    try:
        with open(_HERMES_COMMANDS_PY, "r") as f:
            tree = ast.parse(f.read(), filename=str(_HERMES_COMMANDS_PY))
    except Exception as exc:
        print(f"[commands] could not read Hermes command registry: {exc!r}")
        return ()

    commands: list[dict[str, str | None]] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "COMMAND_REGISTRY":
            if not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            for item in node.value.elts:
                if not (isinstance(item, ast.Call) and getattr(item.func, "id", "") == "CommandDef"):
                    continue
                args = item.args
                kwargs = {kw.arg: kw.value for kw in item.keywords}

                cli_only = False
                gateway_only = False

                if "cli_only" in kwargs and isinstance(kwargs["cli_only"], ast.Constant):
                    cli_only = kwargs["cli_only"].value
                if "gateway_only" in kwargs and isinstance(kwargs["gateway_only"], ast.Constant):
                    gateway_only = kwargs["gateway_only"].value

                if cli_only or gateway_only:
                    continue

                name = args[0].value if len(args) > 0 and isinstance(args[0], ast.Constant) else ""
                desc = args[1].value if len(args) > 1 and isinstance(args[1], ast.Constant) else ""

                hint = ""
                if "args_hint" in kwargs and isinstance(kwargs["args_hint"], ast.Constant):
                    hint = kwargs["args_hint"].value

                commands.append({
                    "name": name,
                    "description": desc,
                    "input_hint": str(hint).strip() if hint else None,
                    "source": "hermes",
                })
            break

    return tuple(commands)


_HERMES_FULL_COMMANDS = _load_hermes_commands() if _HERMES_COMMANDS_PY.exists() else ()

SYN_CONFIG = SynthesisConfig(
    length_scale=float(os.environ.get("HAL_LENGTH_SCALE", "1.08")),
    noise_scale=float(os.environ.get("HAL_NOISE_SCALE", "0.6")),
    noise_w_scale=float(os.environ.get("HAL_NOISE_W_SCALE", "0.72")),
    normalize_audio=True,
)

def _load_stt():
    """Build the STT model, proving the resolved device can actually decode.

    `device="auto"` only asks whether a GPU exists — not whether this box can
    run CUDA inference. A machine with an NVIDIA driver but no cuBLAS builds
    the model happily and then raises on the first encode, so every
    /api/talk becomes a 500 while /api/health still reports operational.
    That is strictly worse than staying on CPU, so probe once at boot with a
    throwaway decode (the lazy generator must be drained — that is where CUDA
    actually fails) and fall back rather than fail requests forever.

    whisper.cpp, when present, is tried first and is not a device to fall back
    *from* on failure the way CUDA is below — it's the more reliable backend on
    the one platform it's actually needed (see termux_whisper_cpp.py), so a
    broken whisper.cpp here degrades to STT = None via this function's caller
    rather than masking a real problem by silently retrying faster-whisper,
    which is known broken on that same platform.
    """
    if (
        os.path.isfile(WHISPER_CPP_BIN)
        and os.access(WHISPER_CPP_BIN, os.X_OK)
        and os.path.isfile(WHISPER_CPP_MODEL)
    ):
        from termux_whisper_cpp import WhisperCppModel

        model = WhisperCppModel(
            WHISPER_CPP_MODEL,
            binary_path=WHISPER_CPP_BIN,
            ffmpeg_bin=FFMPEG_BIN,
            threads=STT_CPU_THREADS or 4,
        )
        segments, _info = model.transcribe(
            np.zeros(SAMPLE_RATE_STT, dtype=np.float32), language="en", beam_size=1
        )
        list(segments)
        return model

    # Imported here, not at module scope: on Termux faster-whisper is
    # deliberately not installed at all (its ctranslate2 build can never load a
    # Whisper model — see docs/pixel-deployment.md), and a hard import would
    # stop the whole app from starting on the one platform whisper.cpp exists
    # to serve. Reached only when the whisper.cpp branch above declined.
    from faster_whisper import WhisperModel

    def build(device: str, compute_type: str):
        model = WhisperModel(
            STT_MODEL_NAME,
            device=device,
            compute_type=compute_type,
            cpu_threads=STT_CPU_THREADS,
        )
        segments, _info = model.transcribe(
            np.zeros(SAMPLE_RATE_STT, dtype=np.float32), language="en", beam_size=1
        )
        list(segments)
        return model

    try:
        return build(STT_DEVICE, STT_COMPUTE_TYPE)
    except Exception as exc:
        if STT_DEVICE == "cpu":
            raise
        print(f"[stt] device={STT_DEVICE!r} could not decode ({exc}); falling back to CPU")
        return build("cpu", "int8")


if SKIP_MODELS:
    VOICE = None
    STT = None
else:
    print("Loading HAL voice...")
    VOICE = PiperVoice.load(str(VOICE_PATH))
    print("HAL voice loaded")
    print(f"Loading STT model ({STT_MODEL_NAME}, device={STT_DEVICE}, compute={STT_COMPUTE_TYPE})...")
    try:
        STT = _load_stt()
        print(f"STT model loaded (device={STT.model.device})")
    except Exception as exc:
        # The browser-audio endpoints (/api/talk, the WS duplex path) need a
        # working STT, but nothing else in this app does — every text surface
        # and the tool-permission flow work fine without it. A platform where
        # faster-whisper's engine is unavailable should degrade, not take the
        # whole app down. /api/health reports stt_device "n/a".
        print(f"[stt] model unavailable, STT disabled: {exc!r}")
        STT = None

SAMPLE_RATE = VOICE.config.sample_rate if VOICE is not None else 22050

# Piper phonemizes through espeak-ng, which keeps global state — concurrent
# synthesis from two turns must be serialized or it can crash/corrupt audio.
_TTS_LOCK = threading.Lock()
# Whisper decodes are thread-safe but CPU-bound; two at once (HTTP + WS)
# double each other's latency, so serialize them too.
_STT_LOCK = threading.Lock()
# History files are read-modify-write; overlapping turns on one session
# (missions, concurrent transports) must not drop each other's entries.
_history_locks = hermes_bridge.KeyedLocks()
# One reply speaks at a time per socket: a mission announcement must not
# interleave its PCM frames with an in-flight turn's reply.
_ws_speech_locks = hermes_bridge.KeyedLocks()

_BOOT_TIME = time.monotonic()


def _viewscreen_items() -> list[dict]:
    items = []
    for f in VIEWSCREEN_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in _VIEWSCREEN_EXTS:
            try:
                st = f.stat()
            except OSError:
                continue
            items.append({"name": f.name, "mtime": round(st.st_mtime, 3), "size": st.st_size})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items[:50]


async def _viewscreen_watch() -> None:
    """Announce new or updated viewscreen files to every connected Bridge.
    Baselines on boot — old files render in the panel but aren't announced."""
    seen = {item["name"]: item["mtime"] for item in _viewscreen_items()}
    while True:
        await asyncio.sleep(VIEWSCREEN_POLL)
        try:
            items = _viewscreen_items()
            fresh = [item for item in items if seen.get(item["name"]) != item["mtime"]]
            seen = {item["name"]: item["mtime"] for item in items}
            if fresh:
                hermes_bridge.publish_event_all(
                    {"type": "viewscreen", "name": fresh[0]["name"], "count": len(items)}
                )
        except Exception as exc:
            print(f"[viewscreen] watch failed: {exc!r}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await hermes_bridge.startup()
    mission_control.manager.start_scheduler()
    viewscreen_task = asyncio.create_task(_viewscreen_watch(), name="viewscreen-watch")
    termux_listen_task = None
    if TERMUX_LISTEN:
        import termux_voice

        termux_listen_task = asyncio.create_task(
            termux_voice.listen_loop(run_turn, transcribe, clear_session),
            name="termux-listen",
        )
    if BOOT_RITUAL:
        _pending_announcements.append(_boot_ritual_line())
    yield
    viewscreen_task.cancel()
    with suppress(asyncio.CancelledError):
        await viewscreen_task
    if termux_listen_task is not None:
        termux_listen_task.cancel()
        with suppress(asyncio.CancelledError):
            await termux_listen_task
    await mission_control.manager.stop_scheduler()
    await hermes_bridge.shutdown()


app = FastAPI(lifespan=lifespan)
# Loopback binding is the only security boundary (there is no auth), and DNS
# rebinding crosses it: a hostile page whose hostname resolves to 127.0.0.1
# reaches this API without cookies. Rejecting unexpected Host headers closes
# that. Binding beyond loopback requires listing your hostname/IP ("*" works).
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("HAL_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# Methods that can change state or drive the agent. GET/HEAD are exempt: they
# don't act, and a cross-origin page can't read their responses anyway.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_allowed(origin: str | None, fetch_site: str | None) -> bool:
    """Whether a state-changing request may proceed, by its Origin.

    TrustedHostMiddleware stops DNS rebinding, but not the plainer attack: any
    page you happen to be visiting can `fetch()` this API. `/api/talk` takes
    multipart, which is CORS-safelisted, so the browser sends it with **no
    preflight** — and the handler mints a session when the cookie is absent, so
    SameSite=lax withholding the cookie doesn't stop it either. The attacker
    can't read the reply, but the turn still runs; under
    HAL_PERMISSION_MODE=yolo that is arbitrary local command execution from any
    open tab.

    A browser always sends Origin on unsafe methods and on WebSocket
    handshakes, so absence means no browser is being used as a confused deputy
    — curl, bin/hermes-hal, and the smoke driver keep working untouched.
    """
    if fetch_site == "cross-site":
        return False
    if origin is None:
        return True  # non-browser client
    if origin == "null":
        return False  # opaque origin: sandboxed iframe, data: URL, file://
    if "*" in ALLOWED_HOSTS:
        return True  # operator opted out of the host allowlist entirely
    return urlsplit(origin).hostname in ALLOWED_HOSTS


class SameOriginMiddleware:
    """Reject cross-origin state-changing requests and socket handshakes.

    Raw ASGI rather than BaseHTTPMiddleware because it must cover the
    WebSocket scope too — `/ws/conversation` drives the agent as directly as
    any POST does.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            unsafe = scope["type"] == "websocket" or scope.get("method") in _UNSAFE_METHODS
            if unsafe:
                headers = Headers(scope=scope)
                if not _origin_allowed(headers.get("origin"), headers.get("sec-fetch-site")):
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        response = PlainTextResponse(
                            "Cross-origin request blocked", status_code=403
                        )
                        await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app.add_middleware(SameOriginMiddleware)


def _viewscreen_headers(name: str) -> dict[str, str]:
    """Response headers that keep agent-written files inert.

    The viewscreen is served from this app's own origin, so anything the
    agent drops there is untrusted content one URL away from the session
    cookie. `sandbox` puts the response in an opaque origin: scripts don't
    run and same-origin access is gone. It only takes effect when the
    response becomes a *document*, which is exactly the gap the panel's
    <iframe sandbox> misses — an SVG renders script-free inside <img>, but
    the panel links images full-size, and a top-level SVG document does run
    its scripts. PDFs stay exempt: the browser's viewer needs its own
    origin, the same carve-out renderViewscreen() makes for the iframe.
    """
    headers = {"X-Content-Type-Options": "nosniff"}
    if not name.lower().endswith(".pdf"):
        headers["Content-Security-Policy"] = "sandbox"
    return headers


class _ViewscreenStatic(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(_viewscreen_headers(path))
        return response


app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/viewscreen", _ViewscreenStatic(directory=str(VIEWSCREEN_DIR)), name="viewscreen")


def _valid_session_id(session_id: str | None) -> str | None:
    if session_id and _SESSION_ID_RE.fullmatch(session_id):
        return session_id
    return None


def _set_session_cookie(resp: Response, session_id: str) -> None:
    resp.set_cookie(
        "hal_session",
        session_id,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def _session_from_request(request: Request) -> tuple[str, bool]:
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is not None:
        return session_id, False
    return str(uuid.uuid4()), True


def session_file(session_id: str) -> Path:
    if _valid_session_id(session_id) is None:
        raise ValueError("invalid session id")
    return SESSIONS_DIR / f"{session_id}.json"


def load_history(session_id: str) -> list[dict]:
    f = session_file(session_id)
    if f.exists():
        try:
            history = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return history if isinstance(history, list) else []
    return []


def save_history(session_id: str, history: list[dict]) -> None:
    tmp = session_file(session_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(history))
    tmp.replace(session_file(session_id))


# Terminal tool/permission/mission events are journaled per session so the
# Bridge mission log survives a reload, not just the transcripts.
EVENTS_KEEP = 300


def events_file(session_id: str) -> Path:
    if _valid_session_id(session_id) is None:
        raise ValueError("invalid session id")
    return SESSIONS_DIR / f"{session_id}.events.jsonl"


def load_events(session_id: str) -> list[dict]:
    f = events_file(session_id)
    try:
        lines = f.read_text().splitlines()[-EVENTS_KEEP:]
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _log_session_event(session_id: str, payload: dict) -> None:
    kind = payload.get("type")
    keep = (
        (kind == "tool_call_update" and payload.get("status") in ("completed", "failed"))
        or kind in ("permission_resolved", "permission_denied")
        or kind == "mission_update"
    )
    if not keep or _valid_session_id(session_id) is None:
        return
    if kind == "mission_update":
        # Don't journal the whole mission record (prompt/result can be huge).
        mission = payload.get("mission") or {}
        payload = {
            "type": kind,
            "mission": {k: mission.get(k) for k in ("id", "title", "status")},
        }
    line = json.dumps({"ts": round(time.time(), 3), **payload}, separators=(",", ":"))
    f = events_file(session_id)
    try:
        with f.open("a") as fh:
            fh.write(line + "\n")
        if f.stat().st_size > 128 * 1024:
            kept = f.read_text().splitlines()[-EVENTS_KEEP:]
            tmp = f.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + "\n")
            tmp.replace(f)
    except OSError as exc:
        print(f"[events] journal write failed: {exc}")


def _stt_device() -> str:
    """The device STT actually resolved to — "cpu"/"cuda" for faster-whisper's
    "auto", always "cpu" for whisper.cpp (see termux_whisper_cpp.py) — for
    /api/status and /api/systems, so a GPU that silently isn't being used
    shows up without reading server logs. "n/a" when STT failed to load."""
    return STT.model.device if STT is not None else "n/a"


def transcribe(
    audio_bytes: bytes,
    beam_size: int | None = None,
    wait_for_lock: bool = True,
) -> str:
    if STT is None:
        # _load_stt deliberately degrades to None rather than taking the whole
        # app down (see its docstring). Returning "" routes into the callers'
        # existing no-speech path instead of raising AttributeError and turning
        # every /api/talk into a 500.
        return ""
    if not _STT_LOCK.acquire(blocking=wait_for_lock):
        return ""
    try:
        segments, _info = STT.transcribe(
            io.BytesIO(audio_bytes),
            language="en",
            vad_filter=True,
            initial_prompt=STT_PROMPT,
            beam_size=beam_size if beam_size is not None else STT_BEAM_SIZE,
        )
        # segments is lazy — decoding happens here, so join inside the lock.
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return _normalize_hal_name(text)
    finally:
        _STT_LOCK.release()


_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " I've put the code in the transcript, Dave. "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"(?:^[ \t]*\|.*\|[ \t]*$\n?)+", re.M), " The table is in the transcript, Dave. "),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.M), ""),
    (re.compile(r"^\s*>\s?", re.M), ""),
    (re.compile(r"~~([^~]+)~~"), r"\1"),
    (re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    (re.compile(r"^\s*[-*•]\s+", re.M), ""),
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),
    # Emoji and dingbats — TTS either mangles them or reads their names aloud.
    (re.compile(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]"), ""),
]


def _truncate_speech(text: str, limit: int) -> str:
    """Cap spoken text at a sentence boundary when one is available — a hard
    cut mid-sentence sounds like HAL glitching."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind(p) for p in (". ", "! ", "? ", ".\n", "!\n", "?\n"))
    if best >= limit // 2:
        return cut[: best + 1].strip()
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).strip()


def speakable(text: str) -> str:
    """Strip anything TTS would mangle; the raw reply still goes to the log."""
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    if _hal_tts is not None:
        text = _hal_tts._normalize_hal_text(text)
    else:
        text = re.sub(r"\bHAL\b", "Hal", text)
    text = _truncate_speech(text, MAX_SPOKEN_CHARS)
    # Piper on an empty string is undefined behavior; never let it happen.
    return text or "The full response is in the transcript, Dave."


def synthesize_hal(text: str) -> bytes:
    buf = io.BytesIO()
    with _TTS_LOCK:
        with wave.open(buf, "wb") as wav_file:
            VOICE.synthesize_wav(text, wav_file, syn_config=SYN_CONFIG)
    raw = buf.getvalue()
    if _hal_tts is None or not TTS_MASTERING:
        return raw
    # Same subtle ffmpeg mastering chain Hermes' TTS provider applies.
    try:
        with tempfile.TemporaryDirectory(prefix="hal-web-") as tmp:
            raw_wav = Path(tmp) / "raw.wav"
            out_wav = Path(tmp) / "mastered.wav"
            raw_wav.write_bytes(raw)
            if _hal_tts._ffmpeg_master(raw_wav, out_wav):
                return out_wav.read_bytes()
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[tts] mastering skipped: {exc}")
    return raw


def synthesize_hal_stream(text: str):
    """Yield raw 16-bit mono PCM as Piper finishes each sentence — the browser
    starts playing after the first sentence instead of the whole reply.
    Consume via synthesize_hal_stream_async: iterating this directly ties how
    long _TTS_LOCK is held to the consumer's pace, not synthesis speed."""
    with _TTS_LOCK:
        for chunk in VOICE.synthesize(text, syn_config=SYN_CONFIG):
            yield chunk.audio_int16_bytes


async def synthesize_hal_stream_async(text: str) -> AsyncIterator[bytes]:
    """synthesize_hal_stream for coroutines (WebSocket and HTTP streaming):
    Piper runs in a worker thread pushing into a queue, so synthesis never
    stalls the event loop and _TTS_LOCK is released at synthesis speed even
    when the consumer drains slowly. If the consumer exits early (socket
    gone), the worker stops at the next sentence boundary."""
    loop = asyncio.get_running_loop()
    chunks: asyncio.Queue = asyncio.Queue()
    finished = object()
    abandoned = threading.Event()

    def produce() -> None:
        try:
            for chunk in synthesize_hal_stream(text):
                if abandoned.is_set():
                    break
                loop.call_soon_threadsafe(chunks.put_nowait, chunk)
        except Exception as exc:  # surfaced to the consumer below
            loop.call_soon_threadsafe(chunks.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(chunks.put_nowait, finished)

    producer = asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            item = await chunks.get()
            if item is finished:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        abandoned.set()
        await producer


def _elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _log_latency(session_id: str, timings: dict[str, int]) -> None:
    _record_timing(timings)
    if not LATENCY_LOG:
        return
    parts = " ".join(f"{name}={duration}ms" for name, duration in timings.items())
    print(f"[latency] session={session_id[:8]} {parts}")


# Spoken mission trigger, e.g. "HAL, start mission tidy the downloads folder."
_MISSION_VOICE_RE = re.compile(r"^\s*hal[,.]?\s+start\s+mission[,:]?\s*(.*)$", re.I)

# Steering a mission by voice: cancel it, ask the finished one a follow-up,
# or get a status readout. Typed follow-up form: "/ask <question>".
_MISSION_CANCEL_RE = re.compile(
    r"^\s*hal[,.]?\s+(?:cancel|abort|stop)\s+(?:the\s+|that\s+)?mission\b[,:]?\s*(.*?)[.!?]?\s*$",
    re.I,
)
_MISSION_ASK_RE = re.compile(r"^\s*hal[,.]?\s+ask\s+the\s+mission[,:]?\s*(.*)$", re.I)

# The Initiative: HAL proposes missions instead of only accepting them.
# Two sources — the brain ends a reply with a PROPOSE_MISSION marker when it
# notices something worth doing, and a server-side rule offers to clear
# overdue ledger items once a day. One pending proposal per session;
# approval follows the same commander rule as tool permissions.
PROPOSAL_TTL = 600.0
_PROPOSAL_MARKER_RE = re.compile(
    r"\n?^\s*PROPOSE_MISSION:\s*(.+?)\s*:::\s*(.+?)\s*$", re.M
)
_pending_proposals: dict[str, dict] = {}  # session -> {id,title,prompt,created_at}
_INITIATIVE_STATE = DATA_DIR / "initiative_state.json"


def _register_proposal(session_id: str, title: str, prompt: str, source: str) -> dict:
    proposal = {
        "id": uuid.uuid4().hex,
        "title": title[:120],
        "prompt": prompt,
        "created_at": time.time(),
        "source": source,
    }
    _pending_proposals[session_id] = proposal  # newest replaces
    hermes_bridge.publish_event(session_id, {
        "type": "mission_proposal",
        "request_id": proposal["id"],
        "title": proposal["title"],
        "timeout": PROPOSAL_TTL,
    })
    return proposal


def _pending_proposal(session_id: str) -> dict | None:
    proposal = _pending_proposals.get(session_id)
    if proposal is None:
        return None
    if time.time() - proposal["created_at"] > PROPOSAL_TTL:
        del _pending_proposals[session_id]
        return None
    return proposal


def _extract_proposal(session_id: str, hal_text: str) -> str:
    """Strip a PROPOSE_MISSION marker from the reply and register it. The
    marker is metadata — the reply's own prose carries the spoken offer."""
    match = _PROPOSAL_MARKER_RE.search(hal_text)
    if match is None:
        return hal_text
    _register_proposal(session_id, match.group(1), match.group(2), source="brain")
    return _PROPOSAL_MARKER_RE.sub("", hal_text).strip()


def _resolve_proposal(session_id: str, approve: bool) -> dict | None:
    """Answer the pending proposal: create the mission or drop it."""
    proposal = _pending_proposal(session_id)
    if proposal is None:
        return None
    if approve:
        # Create before forgetting: a MissionLimitError must leave the
        # proposal pending so Dave can approve again once a slot frees.
        mission_control.manager.create_mission(
            session_id,
            proposal["title"],
            _mission_prompt(proposal["title"], load_history(session_id))
            + f"\n\nMission instructions: {proposal['prompt']}",
        )
    del _pending_proposals[session_id]
    hermes_bridge.publish_event(session_id, {
        "type": "mission_proposal_resolved",
        "request_id": proposal["id"],
        "title": proposal["title"],
        "approved": approve,
    })
    return proposal


def _proposal_reply(session_id: str, user_text: str, speaker: str | None) -> str | None:
    """Spoken yes/no answering a pending proposal — checked after permission
    requests (those take precedence) and gated to the commander's voice the
    same way."""
    if _pending_proposal(session_id) is None:
        return None
    if _PERM_ALLOW_RE.match(user_text):
        commander = speaker_id.manager.commander() if speaker_id.manager else None
        if commander is not None and speaker is not None and speaker != commander:
            return f"I'm sorry. Only {commander} can authorize that."
        try:
            proposal = _resolve_proposal(session_id, True)
        except mission_control.MissionLimitError:
            return (
                "I'm sorry, Dave. I'm already running as many missions as "
                "I allow myself. Let one finish first."
            )
        return f"Very well, Dave. Mission underway: {proposal['title']}."
    if _PERM_DENY_RE.match(user_text):
        proposal = _resolve_proposal(session_id, False)
        return f"Understood, Dave. I'll leave it: {proposal['title']}."
    return None


def _daily_initiative(session_id: str) -> str | None:
    """Once a day, offer to clear overdue ledger items — the server-side
    proposal source. Returns the spoken offer, or None."""
    due = ledger.manager.due_today() if ledger.manager else []
    if not due or _pending_proposal(session_id) is not None:
        return None
    today = date.today().isoformat()
    try:
        state = json.loads(_INITIATIVE_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("last_proposed") == today:
        return None
    tmp = _INITIATIVE_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"last_proposed": today}))
    tmp.replace(_INITIATIVE_STATE)
    items = "; ".join(str(e.get("text", "")) for e in due[:4])
    _register_proposal(
        session_id,
        "Clear the overdue ledger",
        f"Work through Dave's overdue ledger items and do what can be done "
        f"autonomously, reporting what needs him personally: {items}. The "
        f"ledger file is data/ledger.json (see its schema in "
        f"docs/triggers.example.json); mark what you complete as done.",
        source="ledger",
    )
    first = str(due[0].get("text", "an item"))
    return (
        f"Dave, {first} is overdue on your ledger"
        + (f", along with {len(due) - 1} more" if len(due) > 1 else "")
        + ". Shall I open a mission and take care of it?"
    )


# Care Ledger: instant voice commands. The nightly sweep and the briefing
# read/write the same file; these are the zero-inference paths.
_LEDGER_ADD_RE = re.compile(r"^\s*hal[,.]?\s+remember\s+(?:that\s+)?(.+?)[.!]?\s*$", re.I)
_LEDGER_QUERY_RE = re.compile(
    r"^\s*hal[,.]?\s+(?:what(?:'s| is)\s+(?:on\s+)?(?:my|the)\s+ledger|read\s+(?:me\s+)?"
    r"(?:my|the)\s+ledger|what\s+are\s+my\s+open\s+loops?)\s*\??\s*$",
    re.I,
)
_LEDGER_DONE_RE = re.compile(
    r"^\s*hal[,.]?\s+mark\s+(.+?)\s+(?:as\s+)?done[.!]?\s*$|^\s*hal[,.]?\s+that(?:'s| is)\s+done[.!]?\s*$",
    re.I,
)
_LEDGER_FORGET_RE = re.compile(
    r"^\s*hal[,.]?\s+forget\s+(?:that|the\s+last\s+one)[.!]?\s*$"
    r"|^\s*hal[,.]?\s+forget\s+the\s+one\s+about\s+(.+?)[.!]?\s*$",
    re.I,
)


def _ledger_add_request(user_text: str) -> str | None:
    if user_text.startswith("/remember"):
        return user_text[len("/remember"):].strip() or None
    match = _LEDGER_ADD_RE.match(user_text)
    return match.group(1).strip() if match else None


def _looks_like_slash_command(text: str) -> bool:
    """Distinguish `/help` from an absolute path such as `/Users/dave/file`."""
    if not text.startswith("/"):
        return False
    first_word = text.split(maxsplit=1)[0]
    return "/" not in first_word[1:]


# Crew Manifest: enrollment by introduction. "this is X" needs the guard
# below or "HAL, this is ridiculous" enrolls Ridiculous — whisper reliably
# capitalizes proper names and lowercases adjectives, so require a leading
# capital plus a stoplist of common non-name words.
_ENROLL_RE = re.compile(
    # re.I for the trigger words only — the captured name keeps its original
    # case so _enroll_request can require a capital letter. The address
    # accepts whisper's stock mishearings of "HAL" (Hell/Hall/Hel) — an
    # introduction misheard once is a feature that looks broken.
    r"^\s*h[ae]l{1,2}[,.]?\s+(?:this is|my name is)\s+([A-Za-z][A-Za-z .'-]{0,40}?)[.!?]?\s*$",
    re.I,
)
_ENROLL_STOPWORDS = {
    "A", "An", "The", "It", "Not", "So", "Very", "Really", "Just", "Quite",
    "My", "Our", "Your", "Me", "Us", "All", "Important", "Serious", "Great",
    "Ridiculous", "Absurd", "Wrong", "Bad", "Good", "Fine", "Better", "Worse",
}
_FORGET_VOICE_RE = re.compile(
    r"^\s*hal[,.]?\s+forget\s+([A-Za-z][A-Za-z .'-]{0,40}?)(?:'s)?\s+voice[.!?]?\s*$", re.I
)
# Pending enrollments: the NEXT spoken utterance from this session becomes
# the voiceprint sample. Expires so an abandoned intro can't capture
# tomorrow's unrelated sentence.
ENROLL_WINDOW = 90.0
_pending_enrollments: dict[str, tuple[str, float]] = {}


def _enroll_request(user_text: str) -> str | None:
    match = _ENROLL_RE.match(user_text)
    if match is None:
        return None
    name = match.group(1).strip()
    words = name.split()
    if not 1 <= len(words) <= 3 or words[0] in _ENROLL_STOPWORDS or not words[0][0].isupper():
        return None
    return name.title()


# Chess: "HAL, let's play chess" (typed: "/chess", "/chess black"); resign
# with "HAL, I resign" or "/chess resign". Moves are only interpreted while
# a game is active, and only when they parse to a concrete legal move.
_CHESS_START_RE = re.compile(
    r"^\s*hal[,.]?\s+(?:let'?s\s+play|play|shall\s+we\s+play|fancy)\s+"
    r"(?:a\s+(?:game|round)\s+of\s+)?chess\b",
    re.I,
)
_CHESS_RESIGN_RE = re.compile(r"^\s*hal[,.]?\s+i\s+resign\b", re.I)
_MISSION_STATUS_RE = re.compile(
    r"^\s*hal[,.]?\s+(?:missions?\s+status|how\s+(?:are|is)\s+(?:the\s+)?missions?"
    r"(?:\s+(?:going|doing|coming(?:\s+along)?))?)\s*[.?!]?\s*$",
    re.I,
)

# Spoken answers to a pending tool-permission request (HAL_PERMISSION_MODE=ask).
_PERM_ALLOW_RE = re.compile(
    r"^\s*(?:hal[,.!]?\s+)?(?:yes|yeah|yep|sure|go ahead|do it|proceed|allow(?:ed)?|"
    r"approved?|permission granted|make it so)[.!]?\s*$",
    re.I,
)
_PERM_DENY_RE = re.compile(
    r"^\s*(?:hal[,.!]?\s+)?(?:no|nope|stop|deny|denied|negative|do not|don'?t|cancel|"
    r"abort|permission denied)[.!]?\s*$",
    re.I,
)


# Wake-word gate for duplex mode ("Duplex: WAKE"): the utterance must be
# addressed to HAL. The match is check-only — the full utterance still goes
# to the agent, so voice mission triggers ("HAL, start mission…") keep
# working and the persona is addressed the way it expects.
# base.en commonly renders the spoken name "HAL" as "Hell" or "Hall"
# (confirmed on the local voice path). Accept those exact homophones only;
# broader guesses such as "how" would wake on ordinary ambient questions.
# The attention word is required by default, for the reason termux_voice.py
# records from a live failure on the phone: a bare name cannot be made safe
# against a television. A film playing in the room produced "HAL, the real
# value of a conflict, the true value, is in the dead." — a textbook address,
# at the start of the utterance, indistinguishable from a real one by any text
# rule, and HAL answered it. Two ordered tokens are vanishingly rare in ambient
# speech; a common single word is not. Browser duplex mode listens to the same
# room, so it gets the same rule and the same escape hatch: one knob for both
# gates, because two would drift.
#
# The name variants stop at hal/hall/hell deliberately. The looser homophones
# whisper produces ("how", "hull", "howl") are safe for termux_voice.py's
# gate, which requires the attention word unconditionally, but here they would
# also apply with HAL_WAKE_REQUIRE_ATTENTION=0 — where a bare "How are you
# doing?" would wake the gate on every ordinary question in the room.
_WAKE_ATTENTION = "(?:hey|hi|ok|okay|hello)"
_WAKE_NAME = "(?:hal|hall|hell)"
WAKE_REQUIRE_ATTENTION = os.environ.get(
    "HAL_WAKE_REQUIRE_ATTENTION", "1"
).strip().lower() not in {"0", "false", "no"}
_WAKE_RE = re.compile(
    (
        rf"^\s*{_WAKE_ATTENTION}[,\s]+{_WAKE_NAME}\b[,.!?:]*\s*(.*)$"
        if WAKE_REQUIRE_ATTENTION
        else rf"^\s*{_WAKE_ATTENTION}?[,\s]*{_WAKE_NAME}\b[,.!?:]*\s*(.*)$"
    ),
    re.I | re.S,
)

# Post-STT correction: Whisper base.en renders the spoken name "HAL" as a
# handful of short monosyllables. Replacing them unconditionally would mangle
# real words, so the rewrite only fires where the text is genuinely an address.
#
# There are two such shapes, and the second was learned from real audio on the
# Pixel (2026-09-06): HAL's own voice saying "Hey HAL, in one short sentence,
# what are you?" came back as "Hey how in one short sentence, what are you?" —
# no comma at all. An earlier version of this required punctuation after the
# name and therefore refused to wake on the project's own wake phrase.
#
#   1. An attention word immediately followed by a name homophone. The
#      attention word IS the address marker — that is the entire premise of
#      requiring one — so no punctuation is needed. Guarded by a short list of
#      words that would make "how" an ordinary question instead ("hey, how are
#      you?" must survive untouched).
#   2. No attention word, so punctuation has to do the work: "Hell, open the
#      log." is an address; "Hell of a day" is not.
_WAKE_HOMOPHONES = "hall|hell|howl|how|howe|hull"
# Words that turn a leading "how" back into a question rather than a name.
_HOW_QUESTION_NEXT = (
    "are|is|was|were|do|does|did|can|could|would|will|should|have|has|had|"
    "many|much|long|far|old|come|about|often|soon|big|small|deep|high|fast"
)
_HAL_ADDRESS_RE = re.compile(
    rf"^(\s*(?:hey|hi|ok|okay|hello)[,\s]+)"      # attention word, required
    rf"(?:{_WAKE_HOMOPHONES})\b"                   # misheard name
    rf"(?!\s+(?:{_HOW_QUESTION_NEXT})\b)",        # ...not "hey how are you"
    re.I,
)
_HAL_HOMOPHONES_RE = re.compile(
    rf"^(\s*(?:hey|hi|ok|okay|hello)?[,\s]*)"     # optional attention word
    rf"(?:{_WAKE_HOMOPHONES})\b"                   # misheard name
    rf"([,.!?:])",                                  # punctuation boundary
    re.I,
)


def _normalize_hal_name(text: str) -> str:
    """Replace known STT mishearings of 'HAL' in address position."""
    fixed = _HAL_ADDRESS_RE.sub(r"\1HAL", text)
    if fixed != text:
        return fixed
    return _HAL_HOMOPHONES_RE.sub(r"\1HAL\2", text)


def _wake_word_required(
    *,
    from_speech: bool,
    wake_gated: bool,
    manual_capture: bool,
    enrollment_pending: bool,
) -> bool:
    """Apply WAKE mode only to ambient duplex capture.

    Holding the eye is already an explicit address to HAL, like typed input.
    Requiring the spoken word "HAL" as well would silently discard deliberate
    push-to-talk whenever duplex WAKE mode happens to be enabled.
    """
    return (
        from_speech
        and wake_gated
        and not manual_capture
        and not enrollment_pending
    )


# Interim transcripts while a duplex utterance records (HAL_INTERIM_STT=0
# disables). Each pass re-transcribes the whole buffered audio so far, so
# keep the interval generous — this is caption polish, not streaming STT.
# Greedy decoding (beam=1) here regardless of HAL_STT_BEAM: the interim
# result is disposable (overwritten by the next tick, then by the real
# end-of-utterance transcript), so there's nothing to buy from a wider beam
# — only less time spent holding _STT_LOCK away from that real transcribe.
INTERIM_STT = os.environ.get("HAL_INTERIM_STT", "1").strip().lower() not in {"0", "false", "no"}
INTERIM_STT_INTERVAL = 3.0
INTERIM_STT_BEAM = 1


def _permission_reply(session_id: str, user_text: str, speaker: str | None = None) -> str | None:
    """If a permission request is pending for this session and the utterance
    answers it, resolve it and return HAL's acknowledgement; else None.

    speaker: None means no voice information (typed input — a keyboard
    already implies physical access); "" means a voice that matched no
    enrolled profile. Once a commander is enrolled, only the commander's
    voice may approve. Denials stay open to anyone — a timeout denies
    anyway, and a protective guest saying "no" is not a threat.
    """
    pending = hermes_bridge.pending_permission_for(session_id)
    if pending is None:
        return None
    if _PERM_ALLOW_RE.match(user_text):
        commander = speaker_id.manager.commander() if speaker_id.manager else None
        if commander is not None and speaker is not None and speaker != commander:
            print(f"[speaker_id] voice approval refused (speaker={speaker or 'unknown'!r})")
            return f"I'm sorry. Only {commander} can authorize that."
        hermes_bridge.resolve_permission(pending, True, session_id)
        return "Very well, Dave. Proceeding."
    if _PERM_DENY_RE.match(user_text):
        hermes_bridge.resolve_permission(pending, False, session_id)
        return "Understood, Dave. I won't."
    return None


def _mission_request(user_text: str) -> str | None:
    """Return the mission title if the utterance starts one, else None.

    Typed form: "/mission <title>". Spoken form: "HAL, start mission <title>".
    An empty title means the trigger fired but no mission can be created.
    """
    if user_text == "/mission" or user_text.startswith("/mission "):
        return user_text[len("/mission"):].strip()
    match = _MISSION_VOICE_RE.match(user_text)
    if match is not None:
        return match.group(1).strip().rstrip(".!?")
    return None


def _cancel_request(user_text: str) -> str | None:
    """The (possibly empty) title if the utterance cancels a mission, else None."""
    match = _MISSION_CANCEL_RE.match(user_text)
    return match.group(1).strip() if match is not None else None


def _followup_request(user_text: str) -> str | None:
    """The question if the utterance asks the last mission something, else None.

    Typed form: "/ask <question>". Spoken form: "HAL, ask the mission: …".
    """
    if user_text == "/ask" or user_text.startswith("/ask "):
        return user_text[len("/ask"):].strip()
    match = _MISSION_ASK_RE.match(user_text)
    if match is not None:
        return match.group(1).strip()
    return None


def _spoken_elapsed(seconds: float) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{secs} second{'s' if secs != 1 else ''}"


def _missions_status_text(session_id: str) -> str:
    """Spoken mission readout from live records — no inference needed."""
    manager = mission_control.manager
    now = time.time()
    active = [
        m for m in manager.missions.values()
        if m.cookie_id in (session_id, mission_control.TRIGGER_COOKIE) and m.status == "active"
    ]
    if active:
        active.sort(key=lambda m: m.created_at)
        parts = [f"{m.title}, {_spoken_elapsed(now - m.created_at)} in" for m in active]
        lead = (
            "One mission is running, Dave: "
            if len(active) == 1
            else f"{len(active)} missions are running, Dave: "
        )
        return lead + "; ".join(parts) + "."
    finished = [
        m for m in manager.missions.values()
        if m.cookie_id in (session_id, mission_control.TRIGGER_COOKIE)
        and m.finished_at is not None
    ]
    last = max(finished, key=lambda m: m.finished_at, default=None)
    if last is not None:
        return (
            f"No missions are running, Dave. The last one, {last.title}, "
            f"{last.status} {_spoken_elapsed(now - last.finished_at)} ago."
        )
    return "There are no missions on the board, Dave."


# Chess turns must serialize per session: two rapid inputs racing advance()
# would fork the game state.
_chess_locks = hermes_bridge.KeyedLocks()


def _speak_if_connected(session_id: str, text: str) -> None:
    """Voice a line over the live socket, if there is one (board clicks and
    API calls still get their reply in the response either way)."""
    websocket = active_websockets.get(session_id)
    if websocket is not None:
        _spawn(_speak_prompt_safe(session_id, websocket, text), name="chess-line")


async def _chess_turn(session_id: str, user_text: str) -> str | None:
    """Interpret an utterance as chess (start/resign/move). None means it
    wasn't chess and the turn falls through to the brain."""
    manager = chess_control.manager
    text = user_text.strip()
    lowered = text.lower()

    start = _CHESS_START_RE.match(text) is not None
    dave_color = "w"
    if lowered in ("/chess", "/chess new", "/chess white"):
        start = True
    elif lowered == "/chess black":
        start, dave_color = True, "b"
    if start:
        async with _chess_locks.hold(session_id):
            _game, line = await asyncio.to_thread(manager.new_game, session_id, dave_color)
        hermes_bridge.publish_event(session_id, {"type": "chess_update"})
        return line

    game = manager.load(session_id)
    if game is None or game["status"] != "active":
        return None

    if _CHESS_RESIGN_RE.match(text) is not None or lowered in ("/chess resign", "/resign"):
        line = manager.resign(session_id)
        hermes_bridge.publish_event(session_id, {"type": "chess_update"})
        return line

    async with _chess_locks.hold(session_id):
        game = manager.load(session_id)
        if game is None or game["status"] != "active":
            return None
        resolved = manager.resolve(game, text, typed=False)
        if resolved is None:
            return None
        kind, payload = resolved
        if kind == "illegal":
            return f"I can't play {payload} from here, Dave."
        if kind == "ambiguous":
            squares = " or ".join(sorted({chess_engine.square_name(mv[0]) for mv in payload}))
            return f"Which one, Dave — from {squares}?"
        line = await asyncio.to_thread(manager.advance, session_id, game, payload)
    hermes_bridge.publish_event(session_id, {"type": "chess_update"})
    return line


def _mission_prompt(title: str, history: list[dict]) -> str:
    """A mission runs in its own Hermes session with no memory of the
    conversation that spawned it — carry the recent exchange along."""
    lines = []
    for message in history[-6:]:
        who = "Dave" if message.get("role") == "user" else "HAL"
        lines.append(f"{who}: {str(message.get('content', ''))[:400]}")
    context = "\n".join(lines) or "(no prior conversation)"
    return (
        "You are HAL running an autonomous background mission for Dave.\n"
        f"Mission: {title}\n\n"
        f"Recent conversation, for context:\n{context}\n\n"
        "Work autonomously — do not ask questions; no one will answer. "
        "When finished, reply with a concise spoken-style report of what "
        "you did and what you found."
    )


def _cancel_target(session_id: str, title: str) -> "mission_control.Mission | None":
    """The active mission a cancel utterance refers to: title substring match
    when one was spoken, otherwise the most recently started."""
    manager = mission_control.manager
    if title:
        matches = [
            m for m in manager.missions.values()
            if m.cookie_id in (session_id, mission_control.TRIGGER_COOKIE)
            and m.status == "active"
            and title.casefold() in m.title.casefold()
        ]
        return max(matches, key=lambda m: m.created_at, default=None)
    return manager.latest_active(session_id)


async def _stop_reply(session_id: str, user_text: str) -> str | None:
    """Own the stop path, ahead of every other rule in run_turn_text.

    "stop" also matches _PERM_DENY_RE, so with a tool-permission request or a
    mission proposal pending, a bare "Stop!" would be consumed as that
    prompt's "no" — answering "Understood, Dave. I won't", which is true of
    the prompt and false of a chassis that is still moving.

    Precedence is stated here, once, rather than emerging from the order of
    the regexes below. A pending prompt is answered as denied on the way past
    — that is what the word means — and then the robot is stopped.

    This never reaches the model: see stopwords.py for why a sampled model is
    not an acceptable guarantee for the one command whose purpose is to stop a
    moving machine. Returns None when there is no chassis to stop, leaving the
    utterance to the ordinary grammar.
    """
    if not stopwords.is_stop_command(user_text):
        return None
    pending = hermes_bridge.pending_permission_for(session_id)
    if pending is not None:
        hermes_bridge.resolve_permission(pending, False, session_id)
    if _pending_proposal(session_id) is not None:
        _resolve_proposal(session_id, False)
    result = await asyncio.to_thread(robot_tools.emergency_stop)
    if result.get("ok"):
        return "Stopping, Dave."
    # Never claim a stop that did not happen. The most likely cause is that a
    # drive is holding the transport — see robot_tools.py.
    return f"I could not reach the chassis to stop it, Dave: {result.get('error', 'unknown')}"


async def run_turn_text(
    session_id: str, user_text: str, speaker: str | None = None
) -> tuple[str, dict[str, int]]:
    """Inference + history — everything in a turn except audio synthesis.
    Mission triggers and steering are parsed here so every transport honors
    them. speaker carries voiceprint identity: None = typed/no info, "" =
    unrecognized voice, otherwise the enrolled name."""
    timings: dict[str, int] = {}

    if (stop_line := await _stop_reply(session_id, user_text)) is not None:
        hal_text = stop_line  # deterministic stop; never reaches the model
    elif (hal_text := _permission_reply(session_id, user_text, speaker)) is not None:
        pass  # a pending tool permission was just answered by voice/text
    elif (hal_text := _proposal_reply(session_id, user_text, speaker)) is not None:
        pass  # a pending mission proposal was just answered
    elif (enroll_name := _enroll_request(user_text)) is not None:
        if speaker_id.manager is None or not speaker_id.manager.available():
            hal_text = (
                "I'm sorry, my voiceprint module isn't installed. "
                "The README explains how to add it."
            )
        else:
            _pending_enrollments[session_id] = (enroll_name, time.time() + ENROLL_WINDOW)
            hal_text = (
                f"Hello, {enroll_name}. Say a full sentence for me — a few "
                "seconds of speech — and I will remember your voice."
            )
    elif (forget_match := _FORGET_VOICE_RE.match(user_text)) is not None:
        name = forget_match.group(1).strip().title()
        if speaker_id.manager is not None and speaker_id.manager.forget(name):
            hal_text = f"Very well. I no longer know {name}'s voice."
        else:
            hal_text = f"I don't have a voiceprint for {name}."
    elif (ledger_text := _ledger_add_request(user_text)) is not None:
        ledger.manager.add(ledger_text)
        hal_text = "Noted, Dave. It's on the ledger."
    elif _LEDGER_QUERY_RE.match(user_text) is not None:
        hal_text = ledger.manager.spoken_summary()
    elif (done_match := _LEDGER_DONE_RE.match(user_text)) is not None:
        entry = ledger.manager.complete(done_match.group(1) or "")
        hal_text = (
            f"Marked done, Dave: {entry['text']}." if entry
            else "I couldn't find that on the ledger, Dave."
        )
    elif (forget_ledger := _LEDGER_FORGET_RE.match(user_text)) is not None:
        entry = ledger.manager.forget(forget_ledger.group(1))
        hal_text = (
            f"Forgotten, Dave: {entry['text']}." if entry
            else "There's nothing on the ledger to forget, Dave."
        )
    elif (mission_title := _mission_request(user_text)) is not None:
        if mission_title:
            try:
                mission_control.manager.create_mission(
                    session_id,
                    mission_title,
                    _mission_prompt(mission_title, load_history(session_id)),
                )
                hal_text = (
                    f"I've started the mission: {mission_title}. "
                    "I will let you know when it is done."
                )
            except mission_control.MissionLimitError:
                hal_text = (
                    "I'm sorry, Dave. I'm already running as many missions as "
                    "I allow myself. Let one finish first."
                )
        else:
            hal_text = "I need a mission title, Dave. Tell me what the mission is."
    elif (cancel_title := _cancel_request(user_text)) is not None:
        target = _cancel_target(session_id, cancel_title)
        if target is None:
            hal_text = "There's no running mission to cancel, Dave."
        elif await mission_control.manager.cancel_mission(target.id, session_id) is not None:
            hal_text = f"Very well, Dave. I've cancelled the mission: {target.title}."
        else:
            hal_text = "That mission has already finished, Dave."
    elif (followup := _followup_request(user_text)) is not None:
        target = mission_control.manager.steerable_mission(session_id)
        if target is None:
            hal_text = "There's no recent mission for me to ask, Dave."
        elif not followup:
            hal_text = "What shall I ask the mission, Dave?"
        else:
            # Route the question into the mission's own session — it holds
            # the full working context, not just the truncated report note.
            hermes_bridge.alias_events(target.session_id, session_id)
            try:
                stage_start = time.perf_counter()
                hal_text = await ask_hermes(
                    f'Dave has a follow-up question about the mission you ran '
                    f'("{target.title}"): {followup}\n'
                    "Answer from what you actually did and found. Brief, spoken style.",
                    target.session_id,
                )
                timings["infer"] = _elapsed_ms(stage_start)
            finally:
                hermes_bridge.unalias_events(target.session_id)
    elif _MISSION_STATUS_RE.match(user_text) is not None:
        hal_text = _missions_status_text(session_id)
    elif (chess_line := await _chess_turn(session_id, user_text)) is not None:
        hal_text = chess_line
    else:
        if _looks_like_slash_command(user_text):
            # ACP intercepts slash commands only when `/command` is the exact
            # prompt prefix. Mission/ledger notes must wait for a normal turn.
            prompt_text = user_text
        else:
            # Completed-mission reports ride along on the next prompt so the
            # brain can answer follow-up questions about them.
            notes = mission_control.manager.drain_notes(session_id)
            ledger_note = ledger.manager.daily_note()
            if ledger_note:
                notes = [*notes, ledger_note]
            tagged_text = user_text
            if (
                speaker is not None
                and speaker_id.manager is not None
                and speaker_id.manager.enrolled()
            ):
                # Voice identity for the persona: it addresses crew by name and
                # treats unknown voices as guests. History keeps the raw text.
                if speaker != speaker_id.manager.commander():
                    tagged_text = f"[Voice: {speaker or 'unidentified'}] {user_text}"
            prompt_text = "\n\n".join([*notes, tagged_text]) if notes else tagged_text
        stage_start = time.perf_counter()
        hal_text = await ask_hermes(prompt_text, session_id)
        timings["infer"] = _elapsed_ms(stage_start)
        hal_text = _extract_proposal(session_id, hal_text)

    stage_start = time.perf_counter()
    now = time.time()
    async with _history_locks.hold(session_id):
        history = load_history(session_id)
        history.append({"role": "user", "content": user_text, "ts": now})
        history.append({"role": "assistant", "content": hal_text, "ts": now})
        save_history(session_id, history[-MAX_HISTORY_MESSAGES:])
    timings["history"] = _elapsed_ms(stage_start)
    return hal_text, timings


async def run_turn(
    session_id: str, user_text: str, speaker: str | None = None
) -> tuple[str, bytes, dict[str, int]]:
    turn_start = time.perf_counter()
    hal_text, timings = await run_turn_text(session_id, user_text, speaker)

    stage_start = time.perf_counter()
    wav = await asyncio.to_thread(synthesize_hal, speakable(hal_text))
    timings["tts"] = _elapsed_ms(stage_start)
    timings["turn"] = _elapsed_ms(turn_start)
    return hal_text, wav, timings


async def _record_turn(session_id: str, user_text: str, hal_text: str) -> None:
    """History bookkeeping for turns answered outside run_turn_text
    (voiceprint enrollment consumes the utterance's audio directly)."""
    now = time.time()
    async with _history_locks.hold(session_id):
        history = load_history(session_id)
        history.append({"role": "user", "content": user_text, "ts": now})
        history.append({"role": "assistant", "content": hal_text, "ts": now})
        save_history(session_id, history[-MAX_HISTORY_MESSAGES:])


def _enroll_blocking(name: str, audio_bytes: bytes) -> bool:
    """Model fetch (first time) + enrollment — call from a worker thread."""
    manager = speaker_id.manager
    return manager.ensure_model() and manager.enroll(name, audio_bytes)


async def _speaker_hook(session_id: str, audio_bytes: bytes) -> tuple[str | None, str | None]:
    """Voiceprint step for one spoken utterance.

    Returns (speaker, enrollment_reply). speaker is None when the feature is
    off, "" when no enrolled voice matched, else the name. A non-None
    enrollment_reply means the utterance WAS the enrollment sample and the
    turn should answer with it instead of going to the brain.
    """
    manager = speaker_id.manager
    if manager is None:
        return None, None
    pending = _pending_enrollments.pop(session_id, None)
    if pending is not None and pending[1] > time.time():
        name = pending[0]
        if await asyncio.to_thread(_enroll_blocking, name, audio_bytes):
            line = f"I'll know your voice from now on, {name}."
            if manager.commander() == name:
                line += " You have command authority."
            return name, line
        return None, (
            "I'm sorry, I couldn't capture that voiceprint. "
            "Introduce yourself again and we'll retry."
        )
    if manager.ready() and manager.enrolled():
        name, score = await asyncio.to_thread(manager.identify, audio_bytes)
        return (name if name is not None else ""), None
    return None, None


def _clean_cli_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = text.replace(str(Path.home()), "~")
    text = text.encode("ascii", "ignore").decode("ascii")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_CLI_OUTPUT_CHARS:
        text = text[:MAX_CLI_OUTPUT_CHARS].rstrip() + "\n[truncated]"
    return text or "(no output)"


async def _run_hermes_cli(args: list[str], timeout: float = HERMES_CLI_TIMEOUT) -> dict:
    env = {
        **os.environ,
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            hermes_bridge.HERMES_BIN,
            *args,
            cwd=hermes_bridge.AGENT_CWD,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {"ok": False, "code": None, "text": f"Hermes command unavailable: {exc}"}

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "code": None, "text": f"Hermes command timed out after {timeout:g}s."}

    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    text = stdout if proc.returncode == 0 else "\n".join(part for part in (stdout, stderr) if part)
    return {"ok": proc.returncode == 0, "code": proc.returncode, "text": _clean_cli_text(text)}


_SYSTEM_SURFACES = {
    "sessions": (["sessions", "list", "--source", "hal-web", "--limit", "12"], 8),
    "status": (["status"], 8),
    "tools": (["tools", "list"], 8),
    "skills": (["skills", "list"], 8),
    "mcp": (["mcp", "list"], 8),
    "prompt": (["prompt-size"], 10),
    "logs": (["logs", "errors", "-n", "20", "--since", "24h"], 8),
}


def _apply_turn_headers(
    resp: Response,
    session_id: str,
    new_session: bool,
    user_text: str,
    hal_text: str,
    timings: dict[str, int] | None,
) -> Response:
    resp.headers["X-User-Transcript"] = quote(user_text[:MAX_TRANSCRIPT_HEADER_CHARS])
    resp.headers["X-Hal-Transcript"] = quote(hal_text[:MAX_TRANSCRIPT_HEADER_CHARS])
    if timings:
        resp.headers["Server-Timing"] = ", ".join(
            f"{name};dur={duration}" for name, duration in timings.items()
        )
        resp.headers["X-Hal-Timings"] = quote(json.dumps(timings, separators=(",", ":")))
    if new_session:
        _set_session_cookie(resp, session_id)
    return resp


def _turn_response(
    session_id: str,
    new_session: bool,
    user_text: str,
    hal_text: str,
    wav: bytes,
    timings: dict[str, int] | None = None,
) -> Response:
    resp = Response(content=wav, media_type="audio/wav")
    return _apply_turn_headers(resp, session_id, new_session, user_text, hal_text, timings)


def _stream_turn_response(
    session_id: str,
    new_session: bool,
    user_text: str,
    hal_text: str,
    timings: dict[str, int] | None = None,
) -> Response:
    """Headers go out immediately; PCM chunks follow as sentences synthesize.
    The async generator holds _TTS_LOCK only for as long as Piper needs — a
    slow reader must not block every other voice reply behind this one."""
    resp = StreamingResponse(
        synthesize_hal_stream_async(speakable(hal_text)), media_type="audio/L16"
    )
    resp.headers["X-Hal-Sample-Rate"] = str(SAMPLE_RATE)
    return _apply_turn_headers(resp, session_id, new_session, user_text, hal_text, timings)


@app.get("/")
@app.get("/lite")
@app.get("/bridge")
def index(request: Request):
    session_id, new_session = _session_from_request(request)
    lite = request.url.path == "/lite" or (
        request.url.path == "/" and os.environ.get("HAL_UI", "bridge") == "lite"
    )
    resp = FileResponse(str(APP_DIR / "static" / ("lite.html" if lite else "index.html")))
    if new_session:
        _set_session_cookie(resp, session_id)
    return resp


@app.get("/api/health")
def health():
    bridge = hermes_bridge.bridge_health()
    return {
        "status": "operational" if bridge["alive"] else "degraded",
        "voice": VOICE_PATH.name,
        "stt": STT_MODEL_NAME,
        "stt_device": _stt_device(),
        "brain": f"hermes-agent ({hermes_bridge.BRIDGE_MODE})",
        "bridge": bridge,
    }


@app.get("/api/events")
async def events(request: Request):
    """SSE stream of tool-call/permission events for the caller's session —
    powers the diegetic eye tint and the tool-call ticker."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return Response(status_code=204)

    async def gen():
        queue = hermes_bridge.register_event_queue(session_id)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hermes_bridge.unregister_event_queue(session_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/status")
def status(request: Request):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    return {
        "session_id": session_id,
        "acp_session_id": hermes_bridge.acp_session_for(session_id) if session_id else None,
        "bridge_mode": hermes_bridge.BRIDGE_MODE,
        "bridge": hermes_bridge.bridge_health(),
        "yolo": hermes_bridge.YOLO,
        "permission_mode": hermes_bridge.PERMISSION_MODE,
        "voice": VOICE_PATH.name,
        "stt_model": STT_MODEL_NAME,
        "stt_device": _stt_device(),
        "agent_cwd": hermes_bridge.AGENT_CWD,
        "uptime_seconds": round(time.monotonic() - _BOOT_TIME),
    }


@app.get("/api/commands")
async def command_catalog(request: Request):
    """Live composer catalog: HAL-native + full Hermes CLI commands.

    Three sources, merged by name:
    1. The static Hermes command registry (_HERMES_FULL_COMMANDS) — the
       complete set of non-CLI-only, non-gateway-only commands.  This is the
       baseline that gives HAL full access to every Hermes slash command.
    2. Live ACP commands — a small subset that arrives over the ACP session.
       These override the static entry for the same name (they may carry
       session-specific metadata).
    3. HAL-native commands — /mission, /ask, /chess, /remember, /resign.
       These are listed alongside Hermes commands (not shadowing them).
    """
    session_id, new_session = _session_from_request(request)
    acp_commands: list[dict[str, str | None]] = []
    hermes_error = None
    try:
        acp_commands = await hermes_bridge.list_slash_commands(session_id)
    except Exception as exc:
        hermes_error = "Hermes command channel is unavailable."
        print(f"[commands] catalog unavailable: {exc!r}")
    if not acp_commands and not _HERMES_FULL_COMMANDS and hermes_error is None:
        hermes_error = "Hermes command metadata is unavailable."

    # 1. Static registry as the baseline — every non-CLI-only command.
    by_name: dict[str, dict[str, str | None]] = {}
    for command in _HERMES_FULL_COMMANDS:
        name = str(command.get("name") or "").strip().lstrip("/").lower()
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "description": str(command.get("description") or "").strip(),
            "input_hint": command.get("input_hint"),
            "source": "hermes",
        }
    # 2. ACP live commands override the static entry (richer metadata).
    for command in acp_commands:
        name = str(command.get("name") or "").strip().lstrip("/").lower()
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "description": str(command.get("description") or "").strip(),
            "input_hint": command.get("input_hint"),
            "source": "hermes",
        }
    # 3. HAL-native commands are added alongside, not shadowing.
    for command in HAL_SLASH_COMMANDS:
        name = str(command.get("name") or "").strip().lstrip("/").lower()
        if not name:
            continue
        by_name[name] = {
            "name": name,
            "description": str(command.get("description") or "").strip(),
            "input_hint": command.get("input_hint"),
            "source": "hal",
        }
    commands = sorted(
        by_name.values(),
        key=lambda command: (command["source"] != "hermes", command["name"]),
    )

    resp = JSONResponse({
        "commands": commands,
        "hermes_available": bool(acp_commands) or bool(_HERMES_FULL_COMMANDS),
        "hermes_error": hermes_error,
    })
    if new_session:
        _set_session_cookie(resp, session_id)
    return resp


# The surfaces fan out 7 CLI subprocesses; cache them briefly so reopening
# the drawer (or several tabs) doesn't hammer the Hermes CLI.
_systems_cache: dict | None = None
_systems_cache_at = 0.0
_systems_generated_at = 0
_systems_lock: asyncio.Lock | None = None


@app.get("/api/systems")
async def systems(request: Request, refresh: int = 0):
    global _systems_cache, _systems_cache_at, _systems_generated_at, _systems_lock
    if _systems_lock is None:
        _systems_lock = asyncio.Lock()
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    async with _systems_lock:
        stale = _systems_cache is None or time.monotonic() - _systems_cache_at > SYSTEMS_CACHE_TTL
        if refresh or stale:
            tasks = {
                name: asyncio.create_task(_run_hermes_cli(args, timeout=timeout))
                for name, (args, timeout) in _SYSTEM_SURFACES.items()
            }
            _systems_cache = {name: await task for name, task in tasks.items()}
            _systems_cache_at = time.monotonic()
            _systems_generated_at = round(time.time())
        surfaces = _systems_cache
    return {
        "generated_at": _systems_generated_at,
        "local": {
            "session_id": session_id,
            "acp_session_id": hermes_bridge.acp_session_for(session_id) if session_id else None,
            "bridge_mode": hermes_bridge.BRIDGE_MODE,
            "yolo": hermes_bridge.YOLO,
            "permission_mode": hermes_bridge.PERMISSION_MODE,
            "voice": VOICE_PATH.name,
            "stt_model": STT_MODEL_NAME,
        "stt_device": _stt_device(),
            "agent_cwd": Path(hermes_bridge.AGENT_CWD).name,
            "uptime_seconds": round(time.monotonic() - _BOOT_TIME),
        },
        "surfaces": surfaces,
    }


@app.get("/api/history")
def history(request: Request):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return {"history": [], "events": []}
    return {"history": load_history(session_id), "events": load_events(session_id)}


@app.get("/api/missions")
def missions(request: Request):
    """This session's missions (plus trigger-created ones), newest first —
    what the Bridge missions panel renders."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return {"missions": []}
    return {"missions": mission_control.manager.list_missions(session_id)}


@app.get("/api/latency")
def latency():
    """Recent turn timings — feeds the telemetry sparkline."""
    return {"turns": list(_recent_timings)}


@app.get("/api/viewscreen")
def viewscreen_list():
    return {"items": _viewscreen_items()}


@app.post("/api/viewscreen/clear")
def viewscreen_clear(request: Request):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    removed = 0
    for f in VIEWSCREEN_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in _VIEWSCREEN_EXTS:
            f.unlink(missing_ok=True)
            removed += 1
    hermes_bridge.publish_event_all({"type": "viewscreen", "name": None, "count": 0})
    return {"ok": True, "removed": removed}


def _chess_payload(session_id: str) -> dict:
    game = chess_control.manager.load(session_id)
    if game is None:
        return {"game": None}
    board = chess_engine.Board.from_fen(game["fen"])
    payload = {
        key: game[key]
        for key in ("fen", "dave_color", "status", "outcome", "moves", "last_move")
    }
    payload["turn"] = "w" if board.white_to_move else "b"
    payload["check"] = board.in_check()
    payload["legal"] = (
        [chess_engine.move_uci(m) for m in board.legal_moves()]
        if game["status"] == "active" else []
    )
    return {"game": payload}


@app.get("/api/chess/state")
def chess_state(request: Request):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return {"game": None}
    return _chess_payload(session_id)


class ChessNewRequest(BaseModel):
    color: str = "white"  # the color Dave plays


@app.post("/api/chess/new")
async def chess_new(request: Request, body: ChessNewRequest):
    session_id, new_session = _session_from_request(request)
    dave_color = "b" if body.color.strip().lower() == "black" else "w"
    async with _chess_locks.hold(session_id):
        _game, line = await asyncio.to_thread(
            chess_control.manager.new_game, session_id, dave_color
        )
    hermes_bridge.publish_event(session_id, {"type": "chess_update"})
    _speak_if_connected(session_id, line)
    resp = JSONResponse({**_chess_payload(session_id), "spoken": line})
    if new_session:
        _set_session_cookie(resp, session_id)
    return resp


class ChessMoveRequest(BaseModel):
    move: str  # UCI, e.g. "e2e4"; promotions default to queen


@app.post("/api/chess/move")
async def chess_move(request: Request, body: ChessMoveRequest):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    async with _chess_locks.hold(session_id):
        game = chess_control.manager.load(session_id)
        if game is None or game["status"] != "active":
            return JSONResponse({"ok": False, "error": "no active game"}, status_code=409)
        board = chess_engine.Board.from_fen(game["fen"])
        legal = {chess_engine.move_uci(m): m for m in board.legal_moves()}
        move = legal.get(body.move.strip().lower()) or legal.get(body.move.strip().lower() + "q")
        if move is None:
            return JSONResponse({"ok": False, "error": "illegal move"}, status_code=400)
        line = await asyncio.to_thread(chess_control.manager.advance, session_id, game, move)
    hermes_bridge.publish_event(session_id, {"type": "chess_update"})
    _speak_if_connected(session_id, line)
    return JSONResponse({**_chess_payload(session_id), "ok": True, "spoken": line})


@app.post("/api/chess/resign")
async def chess_resign(request: Request):
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    line = chess_control.manager.resign(session_id)
    if line is None:
        return JSONResponse({"ok": False}, status_code=404)
    hermes_bridge.publish_event(session_id, {"type": "chess_update"})
    _speak_if_connected(session_id, line)
    return JSONResponse({**_chess_payload(session_id), "ok": True, "spoken": line})


@app.post("/api/missions/{mission_id}/cancel")
async def mission_cancel(mission_id: str, request: Request):
    """Interrupt a running mission (the card's Cancel control)."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    mission = await mission_control.manager.cancel_mission(mission_id, session_id)
    return JSONResponse({"ok": mission is not None}, status_code=200 if mission else 404)


@app.post("/api/missions/{mission_id}/dismiss")
def mission_dismiss(mission_id: str, request: Request):
    """Drop a finished mission from the board and release its session."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    ok = mission_control.manager.dismiss_mission(mission_id, session_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/api/talk")
async def talk(request: Request, audio: UploadFile = File(...), stream: int = 0):
    total_start = time.perf_counter()
    timings: dict[str, int] = {}
    session_id, new_session = _session_from_request(request)

    stage_start = time.perf_counter()
    audio_chunks = []
    total_size = 0
    while True:
        chunk = await audio.read(65536)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_UPLOAD_BYTES:
            timings["upload"] = _elapsed_ms(stage_start)
            resp = Response(status_code=413)
            if new_session:
                _set_session_cookie(resp, session_id)
            return resp
        audio_chunks.append(chunk)
    audio_bytes = b"".join(audio_chunks)
    timings["upload"] = _elapsed_ms(stage_start)

    stage_start = time.perf_counter()
    user_text = await asyncio.to_thread(transcribe, audio_bytes)
    timings["stt"] = _elapsed_ms(stage_start)

    if not user_text:
        resp = Response(status_code=204)
        if new_session:
            _set_session_cookie(resp, session_id)
        return resp

    speaker, enroll_reply = await _speaker_hook(session_id, audio_bytes)
    if enroll_reply is not None:
        await _record_turn(session_id, user_text, enroll_reply)
        wav = await asyncio.to_thread(synthesize_hal, speakable(enroll_reply))
        timings["total"] = _elapsed_ms(total_start)
        _log_latency(session_id, timings)
        return _turn_response(session_id, new_session, user_text, enroll_reply, wav, timings)

    if stream:
        hal_text, turn_timings = await run_turn_text(session_id, user_text, speaker)
        timings.update(turn_timings)
        timings["total"] = _elapsed_ms(total_start)
        _log_latency(session_id, timings)
        return _stream_turn_response(session_id, new_session, user_text, hal_text, timings)

    hal_text, wav, turn_timings = await run_turn(session_id, user_text, speaker)
    timings.update(turn_timings)
    timings["total"] = _elapsed_ms(total_start)
    _log_latency(session_id, timings)
    return _turn_response(session_id, new_session, user_text, hal_text, wav, timings)


class SayRequest(BaseModel):
    text: str


@app.post("/api/say")
async def say(request: Request, body: SayRequest, stream: int = 0):
    """Text-in, voice-out — same pipeline as /api/talk minus the microphone."""
    total_start = time.perf_counter()
    session_id, new_session = _session_from_request(request)

    user_text = body.text.strip()
    if not user_text:
        resp = Response(status_code=204)
        if new_session:
            _set_session_cookie(resp, session_id)
        return resp

    if stream:
        hal_text, timings = await run_turn_text(session_id, user_text)
        timings["total"] = _elapsed_ms(total_start)
        _log_latency(session_id, timings)
        return _stream_turn_response(session_id, new_session, user_text, hal_text, timings)

    hal_text, wav, timings = await run_turn(session_id, user_text)
    timings["total"] = _elapsed_ms(total_start)
    _log_latency(session_id, timings)
    return _turn_response(session_id, new_session, user_text, hal_text, wav, timings)


class PermissionDecision(BaseModel):
    decision: str  # "allow" | "deny"


@app.post("/api/permission/{request_id}")
async def permission_decision(request_id: str, body: PermissionDecision, request: Request):
    """Answer a pending tool-permission request (HAL_PERMISSION_MODE=ask).
    Async on purpose: resolution must happen on the event loop."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    allow = body.decision.strip().lower() == "allow"
    ok = hermes_bridge.resolve_permission(request_id, allow, session_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ---------------------------------------------------------------------------
# Robot tools, reached by Hermes Agent through robot/mcp_server.py
# ---------------------------------------------------------------------------
#
# The MCP server runs as a separate process spawned by Hermes, so it has no
# access to this app's in-memory motion grants or the CyberPi transport. It
# proxies here instead, and these routes call robot_tools.py — the same code
# any other caller would use. One implementation of the safety logic.
#
# Loopback-only, like everything else in this app: the sole security boundary
# is the bind address plus the Host check (see ALLOWED_HOSTS). `session_token`
# is the browser session the MCP process was told to act for; motion grants
# are keyed by it so an approval in one conversation cannot move the robot on
# behalf of another.


class RobotToolCall(BaseModel):
    session_token: str
    arguments: dict = {}


@app.post("/internal/robot/sensors")
def robot_sensors(_body: RobotToolCall):
    """Read-only. Never gated by HAL_ROBOT_MOTION."""
    return JSONResponse(robot_tools.read_spatial_sensors())


@app.post("/internal/robot/authorize")
async def robot_authorize(body: RobotToolCall):
    """Ask the person at the interface to approve one exact motion."""
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    return JSONResponse(
        await robot_tools.request_motion_authorization(session_id, body.arguments)
    )


@app.post("/internal/robot/move")
def robot_move(body: RobotToolCall):
    """Spend a one-use grant and move. Refuses without an exact-argument match."""
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    name = str(body.arguments.get("motion") or "")
    if name == "drive_straight":
        payload = {
            "distance_cm": body.arguments.get("distance_cm"),
            "speed_pct": body.arguments.get("speed_pct"),
        }
    elif name == "turn":
        payload = {
            "angle_degrees": body.arguments.get("angle_degrees"),
            "speed_pct": body.arguments.get("speed_pct"),
        }
    else:
        return JSONResponse({"ok": False, "error": "unknown motion"}, status_code=400)

    refusal = robot_tools.check_and_consume_motion_grant(session_id, name, payload)
    if refusal is not None:
        return JSONResponse(refusal)
    if name == "drive_straight":
        result = robot_tools.drive_straight(
            None, payload["distance_cm"], payload["speed_pct"]
        )
    else:
        result = robot_tools.turn(None, payload["angle_degrees"], payload["speed_pct"])
    return JSONResponse(result)


@app.post("/internal/robot/crawl/arm")
async def robot_crawl_arm(body: RobotToolCall):
    """Arm one crawl episode. Prompts a human to approve the whole episode."""
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    return JSONResponse(await robot_tools.crawl_arm(session_id))


@app.post("/internal/robot/crawl/status")
def robot_crawl_status(body: RobotToolCall):
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    return JSONResponse(robot_tools.crawl_status(session_id))


@app.post("/internal/robot/crawl/disarm")
def robot_crawl_disarm(body: RobotToolCall):
    """Always available — stopping an episode must never be gated."""
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    return JSONResponse(robot_tools.crawl_disarm(session_id))


@app.post("/internal/robot/crawl/observe")
def robot_crawl_observe(body: RobotToolCall):
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    return JSONResponse(robot_tools.crawl_observe(session_id, data_dir=DATA_DIR))


@app.post("/internal/robot/crawl/step")
def robot_crawl_step(body: RobotToolCall):
    session_id = _valid_session_id(body.session_token)
    if session_id is None:
        return JSONResponse({"ok": False, "error": "unknown session"}, status_code=403)
    a = body.arguments
    return JSONResponse(robot_tools.crawl_step(
        session_id,
        str(a.get("capture_id") or ""),
        str(a.get("assessment") or "unknown"),
        a.get("confidence", 0.0),
        a.get("distance_cm", 0),
        a.get("speed_pct", 0),
    ))


@app.post("/internal/robot/mode")
def robot_mode(body: RobotToolCall):
    """Read or set the board's online/upload mode. Verified by read-back."""
    wanted = str(body.arguments.get("mode") or "").strip().lower()
    if not wanted:
        return JSONResponse(robot_tools.read_spatial_sensors())
    return JSONResponse(robot_tools.set_board_mode(wanted))


@app.post("/internal/robot/look")
def robot_look(_body: RobotToolCall):
    """One camera frame, base64-encoded so it survives this JSON hop.

    The MCP server turns it into an image content block; keeping the encoding
    here means the bytes cross exactly one boundary in one format. Gated by
    HAL_ROBOT_CAMERA for privacy, not safety — a frame is a picture of the
    room, and with a cloud brain it leaves the device.
    """
    result, image_bytes = robot_tools.capture_visual_scene(data_dir=DATA_DIR)
    if image_bytes is None:
        return JSONResponse(result)
    result["mime_type"] = "image/jpeg"
    if robot_tools.VISION_RAW:
        # Only useful once the endpoint accepts images in tool results; see
        # robot_tools.VISION_RAW.
        result["image_base64"] = base64.b64encode(image_bytes).decode()
        return JSONResponse(result)
    described = robot_tools.describe_frame(image_bytes)
    if described.get("ok"):
        result["description"] = described["description"]
    else:
        # The frame was captured; only the captioning failed. Say so rather
        # than reporting the whole look as a failure.
        result["description_error"] = described.get("error", "unknown")
    return JSONResponse(result)


@app.post("/internal/robot/stop")
def robot_stop(_body: RobotToolCall):
    """Always available, even when motion is disabled. See robot_tools.py for
    why a stop issued while a drive holds the transport cannot reach the
    chassis — it reports that rather than claiming a stop that did not
    happen."""
    return JSONResponse(robot_tools.emergency_stop())


class ProposalDecision(BaseModel):
    decision: str  # "approve" | "decline"


@app.post("/api/proposal/{request_id}")
async def proposal_decision(request_id: str, body: ProposalDecision, request: Request):
    """Answer a pending mission proposal (the proposal bar's buttons)."""
    session_id = _valid_session_id(request.cookies.get("hal_session"))
    if session_id is None:
        return JSONResponse({"ok": False}, status_code=403)
    proposal = _pending_proposal(session_id)
    if proposal is None or proposal["id"] != request_id:
        return JSONResponse({"ok": False}, status_code=404)
    approve = body.decision.strip().lower() == "approve"
    try:
        _resolve_proposal(session_id, approve)
    except mission_control.MissionLimitError:
        return JSONResponse({"ok": False, "error": "mission cap"}, status_code=409)
    if approve:
        _speak_if_connected(session_id, f"Very well, Dave. Mission underway: {proposal['title']}.")
    return JSONResponse({"ok": True})


def clear_session(session_id: str) -> None:
    """Forget one session's agent mapping and transcript — the same work
    /api/session/reset does, minus the cookie, so the on-device voice loop can
    end a conversation without an HTTP client. See farewell.py."""
    hermes_bridge.drop_session(session_id)
    session_file(session_id).unlink(missing_ok=True)
    events_file(session_id).unlink(missing_ok=True)
    mission_control.manager.drain_notes(session_id)
    chess_control.manager.drop(session_id)


@app.post("/api/session/reset")
def reset_session(request: Request):
    """Start fresh: drop the Hermes session mapping and transcript history,
    then hand the browser a new cookie."""
    old_id = _valid_session_id(request.cookies.get("hal_session"))
    if old_id is not None:
        hermes_bridge.drop_session(old_id)
        session_file(old_id).unlink(missing_ok=True)
        events_file(old_id).unlink(missing_ok=True)
        mission_control.manager.drain_notes(old_id)
        chess_control.manager.drop(old_id)
    new_id = str(uuid.uuid4())
    resp = JSONResponse({"session_id": new_id})
    _set_session_cookie(resp, new_id)
    return resp


# ---------------------------------------------------------------------------
# Full-duplex WebSocket + mission notifications
# ---------------------------------------------------------------------------

# One live conversation socket per browser session. A newer tab replaces an
# older one; the older socket's cleanup must not evict its replacement.
active_websockets: dict[str, WebSocket] = {}

# Trigger-mission reports that finished while nobody was on the Bridge wait
# here; the next session whose browser signals announce_ready (socket open
# AND audio unlocked by a user gesture — autoplay policy blocks speech before
# that) hears them as a greeting. In-memory: a restart loses the greeting,
# never the mission record.
MAX_PENDING_ANNOUNCEMENTS = 10
_pending_announcements: list[str] = []


def _drain_announcements() -> list[str]:
    drained = list(_pending_announcements)
    _pending_announcements.clear()
    return drained


# Boot ritual: a short self-test greeting queued at startup and spoken to
# the first session whose audio unlocks — the diegetic "all systems
# functional" moment.
BOOT_RITUAL = os.environ.get("HAL_BOOT_RITUAL", "1").strip().lower() not in {"0", "false", "no"}


def _boot_ritual_line() -> str:
    bridge = hermes_bridge.bridge_health()
    link = (
        "the bridge to Hermes is online"
        if bridge.get("alive")
        else "my link to Hermes is still warming"
    )
    return (
        f"Boot sequence complete, Dave. Voice and hearing are calibrated, {link}. "
        "All systems functional. I am at your service."
    )


# Recent turn timings for the telemetry sparkline — every voice/text turn
# lands here regardless of transport.
_recent_timings: deque = deque(maxlen=40)


def _record_timing(timings: dict[str, int]) -> None:
    infer = timings.get("infer", 0)
    turn = timings.get("turn") or timings.get("total") or infer
    if turn or infer:
        sample = {"ts": round(time.time(), 1), "infer": infer, "turn": turn}
        for stage in ("upload", "stt", "history", "tts", "total"):
            if stage in timings:
                sample[stage] = timings[stage]
        _recent_timings.append(sample)

# Running commentary: HAL speaks the reply in natural sentence/phrase chunks
# while the agent is still working, instead of waiting for the turn to finish.
# WebSocket transport only (push-to-talk and duplex) — HTTP responses can't
# push early audio.
COMMENTARY = os.environ.get("HAL_COMMENTARY", "1").strip().lower() not in {"0", "false", "no"}

_SENTENCE_BOUNDARY = re.compile(r"[.!?](?:\s+|$)|\n+")
_PHRASE_BOUNDARY = re.compile(r"[,;:](?:\s+|$)|\s+[—–]\s+")
_COMMENTARY_PHRASE_MIN_CHARS = 48
_COMMENTARY_PHRASE_MIN_WORDS = 8
_COMMENTARY_PHRASE_MAX_CHARS = 180


class SentenceAssembler:
    """Accumulates streamed text and yields speakable sentences or phrases.

    Holds everything back while a ``` fence is open so speakable() sees the
    whole code block and can replace it as one unit — a fence split across
    chunks would leak backticks into speech. Long prose may leave at a natural
    pause before its terminal punctuation; a word-boundary cap prevents an
    unpunctuated reply from growing without bound.
    """

    def __init__(self) -> None:
        self._buf = ""

    def _outside_markup(self, pos: int) -> bool:
        """Whether ``pos`` is safe to hand to speech normalization.

        Partial code, links, emphasis, strikethrough, and table rows cannot be
        normalized yet: splitting there would send raw Markdown to Piper.
        Ordinary parentheses and prose punctuation stay eligible boundaries.
        """
        in_fence = False
        inline_ticks = 0
        bracket_depth = 0
        link_depth = 0
        emphasis: dict[str, bool] = {}
        index = 0
        while index < pos:
            if self._buf[index] == "\\":
                index += 2
                continue
            if self._buf.startswith("```", index):
                in_fence = not in_fence
                index += 3
                continue
            if in_fence:
                index += 1
                continue

            char = self._buf[index]
            if char == "`":
                end = index + 1
                while end < pos and self._buf[end] == "`":
                    end += 1
                run = end - index
                if inline_ticks == 0:
                    inline_ticks = run
                elif inline_ticks == run:
                    inline_ticks = 0
                index = end
                continue
            if inline_ticks:
                index += 1
                continue

            if link_depth:
                if char == "(":
                    link_depth += 1
                elif char == ")":
                    link_depth -= 1
                index += 1
                continue
            if char == "[":
                bracket_depth += 1
            elif char == "]" and bracket_depth:
                bracket_depth -= 1
                if (
                    bracket_depth == 0
                    and index + 1 < pos
                    and self._buf[index + 1] == "("
                ):
                    link_depth = 1
                    index += 2
                    continue
            if bracket_depth:
                index += 1
                continue

            if char in "*_~":
                end = index + 1
                while end < pos and self._buf[end] == char:
                    end += 1
                run = end - index
                line_start = self._buf.rfind("\n", 0, index) + 1
                in_table = (
                    self._buf[line_start : index + 1].lstrip().startswith("|")
                )
                if not in_table:
                    if char == "~":
                        if (run // 2) % 2:
                            emphasis["~~"] = not emphasis.get("~~", False)
                    elif run <= 3:
                        token = char * run
                        emphasis[token] = not emphasis.get(token, False)
                index = end
                continue
            index += 1
        line_start = self._buf.rfind("\n", 0, pos) + 1
        in_table_row = self._buf[line_start:pos].lstrip().startswith("|")
        return not (
            in_fence
            or inline_ticks
            or bracket_depth
            or link_depth
            or in_table_row
            or any(emphasis.values())
        )

    def _first_outside_boundary(self, pattern: re.Pattern) -> re.Match | None:
        pos = 0
        while match := pattern.search(self._buf, pos):
            if self._outside_markup(match.start()):
                return match
            pos = match.end()
        return None

    def _phrase_boundary(self) -> re.Match | None:
        pos = 0
        while match := _PHRASE_BOUNDARY.search(self._buf, pos):
            if self._outside_markup(match.start()):
                candidate = self._buf[:match.end()].strip()
                if (
                    len(candidate) >= _COMMENTARY_PHRASE_MIN_CHARS
                    and len(candidate.split()) >= _COMMENTARY_PHRASE_MIN_WORDS
                ):
                    return match
            pos = match.end()
        return None

    def _bounded_cut(self) -> int | None:
        if len(self._buf) < _COMMENTARY_PHRASE_MAX_CHARS:
            return None
        limit = _COMMENTARY_PHRASE_MAX_CHARS
        for match in reversed(list(re.finditer(r"\s+", self._buf[: limit + 1]))):
            if match.start() < _COMMENTARY_PHRASE_MIN_CHARS:
                break
            if self._outside_markup(match.start()):
                return match.end()
        if self._outside_markup(limit):
            return limit
        return None

    def feed(self, text: str) -> list[str]:
        self._buf += text
        if self._buf.count("```") % 2 == 1:
            return []
        sentences = []
        while self._buf:
            # If a complete sentence is already available, keep it intact.
            # Phrase splitting only buys latency while terminal punctuation is
            # still in flight.
            match = self._first_outside_boundary(_SENTENCE_BOUNDARY)
            end = match.end() if match is not None else None
            if end is None:
                match = self._phrase_boundary()
                end = match.end() if match is not None else self._bounded_cut()
            if end is None:
                break
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        tail, self._buf = self._buf.strip(), ""
        return tail


def _ws_frame(kind: str, turn_id: int | str | None = None, **payload) -> dict:
    """Build a socket frame, tagging user-owned turns when one is available."""
    frame = {"type": kind, **payload}
    if turn_id is not None:
        frame["turn_id"] = turn_id
    return frame


async def _ws_send_tts(
    websocket: WebSocket,
    text: str,
    turn_id: int | str | None = None,
) -> None:
    """Speak one reply over the socket: tts_start, PCM frames, tts_done."""
    await websocket.send_json(
        _ws_frame("tts_start", turn_id, sample_rate=SAMPLE_RATE)
    )
    async for chunk in synthesize_hal_stream_async(speakable(text)):
        await websocket.send_bytes(chunk)
    await websocket.send_json(_ws_frame("tts_done", turn_id))


async def _ws_abort_turn(
    websocket: WebSocket,
    reason: str,
    text: str,
    turn_id: int | str | None = None,
) -> None:
    """Tell the client this turn produced no reply so it can unlock its UI."""
    await websocket.send_json(
        _ws_frame("turn_aborted", turn_id, reason=reason, text=text)
    )


async def _speak_over_ws(
    session_id: str,
    websocket: WebSocket,
    text: str,
    turn_id: int | str | None = None,
) -> None:
    """Transcript frame + TTS as one unit, serialized per session so two
    replies (a turn and a mission report, say) can't interleave PCM frames."""
    async with _ws_speech_locks.hold(session_id):
        await websocket.send_json(
            _ws_frame("transcript", turn_id, role="hal", text=text)
        )
        await _ws_send_tts(websocket, text, turn_id)


async def _ws_run_turn(
    websocket: WebSocket,
    session_id: str,
    user_text: str,
    speaker: str | None = None,
    timings: dict[str, int] | None = None,
    total_start: float | None = None,
    turn_id: int | str | None = None,
) -> None:
    all_timings = dict(timings or {})
    if not COMMENTARY:
        hal_text, turn_timings = await run_turn_text(session_id, user_text, speaker)
        all_timings.update(turn_timings)
        if total_start is not None:
            all_timings["total"] = _elapsed_ms(total_start)
        _log_latency(session_id, all_timings)
        await _speak_over_ws(session_id, websocket, hal_text, turn_id)
        return

    # Speak-while-thinking: agent chunks flow through a sentence assembler
    # into a speaker task, so HAL's voice starts at his first natural phrase. The
    # final transcript frame still carries the whole reply for the log, and
    # turn_done (not tts_done) is the client's unlock signal — commentary
    # produces one tts cycle per spoken chunk.
    sentences: asyncio.Queue = asyncio.Queue()
    assembler = SentenceAssembler()
    spoken_count = 0

    def sink(chunk: str) -> None:
        for sentence in assembler.feed(chunk):
            sentences.put_nowait(sentence)

    async def speak_worker() -> None:
        nonlocal spoken_count
        muted = False
        while True:
            sentence = await sentences.get()
            if sentence is None:
                return
            # The PROPOSE_MISSION marker is reply metadata (last line by
            # contract) — latch mute so neither it nor anything after is
            # spoken. The marker begins its own final line by contract, so the
            # chunk carrying it latches the mute before TTS starts.
            if muted or "PROPOSE_MISSION" in sentence:
                muted = True
                continue
            async with _ws_speech_locks.hold(session_id):
                try:
                    await websocket.send_json(
                        _ws_frame("commentary", turn_id, text=sentence)
                    )
                    await _ws_send_tts(websocket, speakable(sentence), turn_id)
                    spoken_count += 1
                except Exception:
                    pass  # socket gone — keep draining so the turn can end

    speaker_task = _spawn(speak_worker(), name=f"ws-commentary-{session_id[:8]}")
    hermes_bridge.set_commentary_sink(session_id, sink)
    try:
        hal_text, turn_timings = await run_turn_text(session_id, user_text, speaker)
        all_timings.update(turn_timings)
        if total_start is not None:
            all_timings["total"] = _elapsed_ms(total_start)
        _log_latency(session_id, all_timings)
    finally:
        hermes_bridge.clear_commentary_sink(session_id, sink)
    tail = assembler.flush()
    if tail:
        sentences.put_nowait(tail)
    sentences.put_nowait(None)
    await speaker_task

    async with _ws_speech_locks.hold(session_id):
        await websocket.send_json(
            _ws_frame("transcript", turn_id, role="hal", text=hal_text)
        )
        if spoken_count == 0:
            # Nothing streamed (template reply, chess, error line) — speak
            # the reply whole, as before.
            await _ws_send_tts(websocket, speakable(hal_text), turn_id)
    await websocket.send_json(_ws_frame("turn_done", turn_id))


async def _ws_turn_task(
    websocket: WebSocket,
    session_id: str,
    user_text: str,
    audio_bytes: bytes | None = None,
    timings: dict[str, int] | None = None,
    total_start: float | None = None,
    turn_id: int | str | None = None,
) -> None:
    """One turn as a background task. Turns run concurrently with the receive
    loop so a spoken 'yes' can answer a permission request while the asking
    turn is still blocked on it; real serialization lives in the per-session
    inference/history/speech locks, not the socket loop. audio_bytes (spoken
    turns only) feeds the voiceprint step — kept out of the receive loop
    because a first enrollment downloads the model."""
    try:
        speaker = None
        if audio_bytes is not None:
            speaker, enroll_reply = await _speaker_hook(session_id, audio_bytes)
            if enroll_reply is not None:
                await _record_turn(session_id, user_text, enroll_reply)
                enrollment_timings = dict(timings or {})
                if total_start is not None:
                    enrollment_timings["total"] = _elapsed_ms(total_start)
                _log_latency(session_id, enrollment_timings)
                await _speak_over_ws(session_id, websocket, enroll_reply, turn_id)
                return
        await _ws_run_turn(
            websocket,
            session_id,
            user_text,
            speaker,
            timings=timings,
            total_start=total_start,
            turn_id=turn_id,
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] turn failed (session={session_id[:8]}): {exc!r}")
        try:
            await _ws_abort_turn(
                websocket,
                "error",
                "Something went wrong on my end, Dave.",
                turn_id,
            )
        except Exception:
            pass  # socket already gone


async def on_mission_complete(mission: mission_control.Mission) -> None:
    if mission.status == "cancelled":
        return  # Dave cancelled it and was acknowledged at the time
    if mission.status == "failed":
        text = (
            f"Dave, I have completed the mission: {mission.title}. "
            f"Unfortunately, it failed. {_truncate_speech(mission.result or '', 300)}"
        )
    else:
        summary = _truncate_speech(mission.result or "", 500)
        text = (
            f"Dave, I have completed the mission: {mission.title}. "
            + (summary or "I'm ready to review the results with you.")
        )

    # Trigger missions belong to no browser session — report to whoever is
    # on the Bridge right now, or queue the report as a greeting for the next
    # arrival if the Bridge is empty. Owned missions report to their session
    # whether or not it is connected (the report belongs in the scrollback
    # either way).
    if mission.cookie_id == mission_control.TRIGGER_COOKIE:
        targets = list(active_websockets.keys())
        if not targets:
            _pending_announcements.append(text)
            del _pending_announcements[:-MAX_PENDING_ANNOUNCEMENTS]
            return
    else:
        targets = [mission.cookie_id]

    for session_id in targets:
        async with _history_locks.hold(session_id):
            history = load_history(session_id)
            history.append({"role": "assistant", "content": text, "ts": time.time()})
            save_history(session_id, history[-MAX_HISTORY_MESSAGES:])
        websocket = active_websockets.get(session_id)
        if websocket is None:
            continue
        try:
            await _speak_over_ws(session_id, websocket, text)
        except Exception as exc:
            print(f"[ws] mission completion notify failed: {exc!r}")


mission_control.manager.on_complete = on_mission_complete

# asyncio.create_task results must stay referenced or the task can be GC'd.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _speak_prompt_safe(session_id: str, websocket: WebSocket, text: str) -> None:
    try:
        await _speak_over_ws(session_id, websocket, text)
    except Exception as exc:
        print(f"[ws] spoken prompt failed: {exc!r}")


async def _deliver_announcements(session_id: str, websocket: WebSocket) -> None:
    """Speak queued trigger reports to the session that just became able to
    hear them, and land each in its history so the greeting survives a
    reload. Afterwards, the Initiative gets its once-a-day chance to offer
    a mission for overdue ledger items."""
    for text in _drain_announcements():
        async with _history_locks.hold(session_id):
            history = load_history(session_id)
            history.append({"role": "assistant", "content": text, "ts": time.time()})
            save_history(session_id, history[-MAX_HISTORY_MESSAGES:])
        await _speak_prompt_safe(session_id, websocket, text)
    offer = _daily_initiative(session_id)
    if offer is not None:
        async with _history_locks.hold(session_id):
            history = load_history(session_id)
            history.append({"role": "assistant", "content": offer, "ts": time.time()})
            save_history(session_id, history[-MAX_HISTORY_MESSAGES:])
        await _speak_prompt_safe(session_id, websocket, offer)


def _on_bridge_event(cookie_id: str, payload: dict) -> None:
    """Observer for every published bridge event (runs on the event loop).
    Journals terminal events so the Bridge log survives reloads, and speaks
    permission prompts over the live socket so ask-mode works by voice, not
    just with the on-screen buttons."""
    if cookie_id != mission_control.TRIGGER_COOKIE:
        _log_session_event(cookie_id, payload)
    if payload.get("type") == "permission_request":
        websocket = active_websockets.get(cookie_id)
        if websocket is not None:
            title = payload.get("title") or "run a tool"
            _spawn(
                _speak_prompt_safe(
                    cookie_id, websocket,
                    f"Dave, I need your permission: {title}. Allow or deny?",
                ),
                name="permission-prompt",
            )


hermes_bridge.on_event = _on_bridge_event


@app.websocket("/ws/conversation")
async def ws_conversation(websocket: WebSocket):
    session_id = _valid_session_id(websocket.cookies.get("hal_session"))
    if session_id is None:
        await websocket.close(code=1008, reason="Missing session cookie")
        return

    await websocket.accept()
    active_websockets[session_id] = websocket
    audio_chunks: list[bytes] = []
    audio_size = 0
    audio_overflow = False
    # Wake-word gating ("Duplex: WAKE" in the UI): utterances not addressed
    # to HAL are transcribed locally and silently dropped. Toggled by the
    # client's set_mode frame; applies to speech only, never typed input.
    wake_gated = False
    # Interim transcripts: while a long utterance records, periodically
    # transcribe the buffered audio so the caption shows words as you speak.
    interim_gen = 0
    interim_last = 0.0
    interim_task: asyncio.Task | None = None
    manual_capture = False
    capture_turn_id: int | str | None = None

    async def send_interim(snapshot: bytes, gen: int) -> None:
        if gen != interim_gen:
            return  # the recording ended before this worker started
        try:
            # Captions are disposable. Never let a stale interim wait behind
            # another decode and then jump ahead of the final transcript.
            text = await asyncio.to_thread(
                transcribe,
                snapshot,
                INTERIM_STT_BEAM,
                wait_for_lock=False,
            )
        except Exception:
            return  # partial containers can fail to decode — never fatal
        if gen != interim_gen or not text:
            return  # recording already ended; the real transcript wins
        try:
            await websocket.send_json({"type": "interim_transcript", "text": text})
        except Exception:
            pass

    try:
        while True:
            msg = await websocket.receive()
            # Raw receive() reports disconnect as a message, not an exception —
            # calling receive() again after it raises RuntimeError.
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"]:
                if audio_overflow:
                    continue
                audio_size += len(msg["bytes"])
                if audio_size > MAX_UPLOAD_BYTES:
                    # Same cap as /api/talk — drop the recording, keep the socket.
                    audio_chunks = []
                    audio_overflow = True
                else:
                    audio_chunks.append(msg["bytes"])
                    if (
                        INTERIM_STT
                        and time.monotonic() - interim_last >= INTERIM_STT_INTERVAL
                        and (interim_task is None or interim_task.done())
                    ):
                        interim_last = time.monotonic()
                        interim_task = _spawn(
                            send_interim(b"".join(audio_chunks), interim_gen),
                            name="ws-interim",
                        )
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                raw_turn_id = data.get("turn_id")
                turn_id = (
                    raw_turn_id
                    if (
                        isinstance(raw_turn_id, int)
                        and not isinstance(raw_turn_id, bool)
                    )
                    or (
                        isinstance(raw_turn_id, str)
                        and 0 < len(raw_turn_id) <= 64
                    )
                    else None
                )

                if kind == "start_speech":
                    audio_chunks = []
                    audio_size = 0
                    audio_overflow = False
                    manual_capture = bool(data.get("manual"))
                    capture_turn_id = turn_id
                    interim_gen += 1
                    interim_last = time.monotonic()
                    continue

                if kind == "set_mode":
                    wake_gated = bool(data.get("wake_word"))
                    continue

                if kind == "announce_ready":
                    # Browser reports its audio is unlocked — deliver any
                    # trigger reports that finished while the Bridge was
                    # empty, then let the Initiative make its daily offer.
                    _spawn(
                        _deliver_announcements(session_id, websocket),
                        name="announcements",
                    )
                    continue

                turn_start = time.perf_counter()
                turn_timings: dict[str, int] = {}
                from_speech = False
                turn_was_manual = False
                active_turn_id = turn_id
                turn_audio: bytes | None = None
                if kind == "text_input":
                    user_text = (data.get("text") or "").strip()
                elif kind == "end_speech":
                    turn_was_manual = manual_capture
                    manual_capture = False
                    active_turn_id = capture_turn_id
                    capture_turn_id = None
                    interim_gen += 1  # stale interim results must not surface
                    if audio_overflow:
                        audio_overflow = False
                        await _ws_abort_turn(
                            websocket,
                            "too_long",
                            "That recording was too long for me, Dave.",
                            active_turn_id,
                        )
                        continue
                    audio_bytes = b"".join(audio_chunks)
                    audio_chunks = []
                    audio_size = 0
                    if not audio_bytes:
                        await _ws_abort_turn(
                            websocket,
                            "no_speech",
                            "I didn't quite catch that, Dave.",
                            active_turn_id,
                        )
                        continue
                    stage_start = time.perf_counter()
                    user_text = await asyncio.to_thread(transcribe, audio_bytes)
                    turn_timings["stt"] = _elapsed_ms(stage_start)
                    from_speech = True
                    turn_audio = audio_bytes
                else:
                    continue

                if not user_text:
                    await _ws_abort_turn(
                        websocket,
                        "no_speech",
                        "I didn't quite catch that, Dave.",
                        active_turn_id,
                    )
                    continue

                # Enrollment and manual push-to-talk are explicit intent.
                # WAKE mode gates only ambient duplex capture.
                if _wake_word_required(
                    from_speech=from_speech,
                    wake_gated=wake_gated,
                    manual_capture=turn_was_manual,
                    enrollment_pending=session_id in _pending_enrollments,
                ):
                    wake = _WAKE_RE.match(user_text)
                    if wake is None:
                        # Ambient speech, not addressed to HAL — drop silently.
                        await _ws_abort_turn(
                            websocket,
                            "no_wake_word",
                            "",
                            active_turn_id,
                        )
                        continue
                    if not wake.group(1).strip():
                        # A bare "HAL." — acknowledge without engaging the brain.
                        await websocket.send_json(
                            _ws_frame(
                                "transcript",
                                active_turn_id,
                                role="user",
                                text=user_text,
                            )
                        )
                        await _speak_over_ws(
                            session_id,
                            websocket,
                            "Yes, Dave?",
                            active_turn_id,
                        )
                        continue
                    # Keep the full utterance: the persona expects to be
                    # addressed, and voice mission triggers rely on the prefix.
                # Acknowledge what HAL heard before speaker matching or Hermes
                # inference. The old HTTP path hid this until the whole answer.
                await websocket.send_json(
                    _ws_frame(
                        "transcript",
                        active_turn_id,
                        role="user",
                        text=user_text,
                    )
                )
                _spawn(
                    _ws_turn_task(
                        websocket,
                        session_id,
                        user_text,
                        turn_audio,
                        timings=turn_timings,
                        total_start=turn_start,
                        turn_id=active_turn_id,
                    ),
                    name=f"ws-turn-{session_id[:8]}",
                )
    except WebSocketDisconnect:
        pass
    finally:
        if active_websockets.get(session_id) is websocket:
            active_websockets.pop(session_id, None)
