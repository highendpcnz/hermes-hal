"""Robot tools and their safety gating for Hermes Hal.

Adapted from hal's `brain/robot_tools.py`, `brain/events.py` (the motion-grant
half) and `robot/mcp_server.py` at commit 1318bd1 — see
docs/technology-transfer.md. Owned here; hal is not consulted again.

**The architecture differs from hal's on purpose.** hal's brains run their own
tool-calling loop in-process, so `GemmaProvider` could call these functions
directly. Hermes Hal's brain is Hermes Agent behind ACP, which owns its own
tool loop, so the chassis is reached the way hal reaches it from Codex: an MCP
server (`robot/mcp_server.py`) proxies to this app's own `/internal/robot/*`
routes, and those routes call the functions here. One implementation of the
safety-relevant logic, not a second copy living in the MCP process.

**Motion is disabled unless HAL_ROBOT_MOTION=1.** Sensors and telemetry are
read-only and always available when hardware is present; anything that turns a
motor is off by default.

**The camera is off unless HAL_ROBOT_CAMERA=1**, and that gate is about
privacy rather than safety. Sensor readings are three numbers; a frame is a
picture of the room, and with a cloud brain it leaves the device to be
inferred on. That should be a deliberate choice, not a side effect of enabling
robot tools. That is not timidity about an unfinished
feature — see the delivery limits below, which are properties of the design
rather than bugs awaiting a fix.

## What the gating does and does not promise

Authorization is two-step and deliberately awkward. The model cannot move the
robot by calling a motion tool; it must first call
`request_motion_authorization` with the *exact* motion, which prompts the
person at the interface, and only an approval mints a **one-use grant bound to
the canonical arguments**. Approving "drive 20 cm" therefore does not authorize
"drive 50 cm", and a grant cannot be spent twice.

What that buys is that no motion happens without a person saying yes to that
specific motion. What it does not buy — and this is inherited from hal's
measured reality, not a gap here:

  * **A stop cannot interrupt a drive in progress.** `run_motion` holds the
    serial transport for the duration of the command, and a second connection
    to the same device fails with "Resource busy" (confirmed live on hal's
    hardware). An emergency stop arriving mid-drive cannot reach the chassis
    and will report that failure honestly rather than claim a stop that did
    not happen.
  * **The on-device voice loop is not listening during motion.**
    `termux_voice.py` is sequential: it is running the turn, not recording.
    A stop shouted mid-drive is never captured in the first place.

What actually keeps this safe is that every motion is bounded and
self-terminates on the firmware side — 50 cm / 30% normally, 5 cm / 10% under
crawl. If that margin ever stops being acceptable, the fix is a concurrent
recorder feeding a stop matcher, or a hardware stop button, which is the honest
answer for a machine that moves. Do not raise the limits to compensate.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Callable

import hermes_bridge

# Motion is opt-in. Read-only sensing is not gated by this.
MOTION_ENABLED = os.environ.get("HAL_ROBOT_MOTION", "0").strip().lower() not in {
    "0",
    "false",
    "no",
    "",
}
ROBOT_PORT = os.environ.get("HAL_ROBOT_PORT", "/dev/ttyUSB0")
MOTION_PERMISSION_TIMEOUT = float(os.environ.get("HAL_MOTION_PERMISSION_TIMEOUT", "60"))


class RobotUnavailable(RuntimeError):
    """No chassis reachable — distinct from a motion that was refused."""


def open_robot_transport(robot_port: str = ""):
    """Open a raw transport to the CyberPi: Android USB fd, or pyserial.

    Returns the bare transport so a drive/turn can read a fresh obstacle
    distance and then send motion over the *same* connection. Two separate
    connections to one serial device fail with "Resource busy" (confirmed live
    on hal's hardware), so this cannot be split into two opens.
    """
    port = robot_port or ROBOT_PORT
    usb_fd = os.environ.get("TERMUX_USB_FD", "").strip()
    if usb_fd:
        from robot.android_usb import Ch340UsbTransport

        return Ch340UsbTransport(int(usb_fd))
    try:
        import serial
    except ImportError as error:
        # On Android the pyserial path is not the one that should have been
        # taken, so "install pyserial" would send you off fixing the wrong
        # thing. The real cause is an unclaimed device: TERMUX_USB_FD is set by
        # `termux-usb -E -r <device>`, and is unset when nothing is claimed.
        if os.environ.get("PREFIX", "").endswith("com.termux/files/usr"):
            raise RobotUnavailable(
                "no USB device claimed: TERMUX_USB_FD is unset. Check the board "
                "is on its own battery and enumerating (`termux-usb -l` must not "
                "be empty), then launch under `termux-usb -E -r <device>`. "
                "See docs/pixel-deployment.md."
            ) from error
        raise RobotUnavailable(
            "pyserial is required for hardware access; see requirements.txt"
        ) from error
    from robot.cyberpi import BAUD_RATE

    transport = serial.Serial(port, BAUD_RATE, timeout=0.05, write_timeout=1.0)
    transport.reset_input_buffer()
    return transport


def _default_opener() -> Callable[[], object]:
    return lambda: open_robot_transport()


# ---------------------------------------------------------------------------
# Read-only sensing
# ---------------------------------------------------------------------------

def read_spatial_sensors(open_transport: Callable[[], object] | None = None) -> dict:
    """One telemetry snapshot. Read-only, so never gated by HAL_ROBOT_MOTION."""
    from robot.telemetry import CyberPiTelemetryClient

    opener = open_transport or _default_opener()
    try:
        transport = opener()
    except Exception as error:
        return {"ok": False, "error": str(error)}
    try:
        client = CyberPiTelemetryClient(transport)
        # initialize() already reads mode and firmware; reporting them costs
        # nothing and stops anyone answering "what mode is the board in?" from
        # a stale log line, which is exactly the mistake this reply prevents.
        bring_up = client.initialize()
        snapshot = client.read_snapshot()
        return {
            "ok": True,
            "ultrasonic_cm": snapshot.ultrasonic_cm,
            "yaw_deg": snapshot.yaw_deg,
            "pitch_deg": snapshot.pitch_deg,
            "mode": getattr(bring_up.mode, "value", str(bring_up.mode)),
            "firmware_version": bring_up.firmware_version,
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}
    finally:
        try:
            transport.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Vision — gated separately from motion
# ---------------------------------------------------------------------------

CAMERA_ENABLED = os.environ.get("HAL_ROBOT_CAMERA", "0").strip().lower() not in {
    "0",
    "false",
    "no",
    "",
}


def _env(value, key: str, default: str):
    return value if value is not None else os.environ.get(key, default)


def capture_frame_app_process(**overrides) -> tuple[bytes, int, int]:
    """The ultra-wide path. `termux-camera-photo` can only address logical
    cameras and always shoots at 1x, which pins it to the main lens; the
    ultra-wide is reachable only by asking camera2 for a zoom ratio below 1.0
    (0.556 on this device, ~104 degrees against the main lens's 71), and
    camera2 needs a real Android runtime. See robot/camera.py."""
    from robot import camera

    return camera.capture_frame_app_process(
        zoom=_env(overrides.get("zoom"), "HAL_CAMERA_ZOOM", "widest"),
        jar_path=_env(overrides.get("jar_path"), "HAL_CAPTURE_JAR", camera.DEFAULT_CAPTURE_JAR),
        app_process_bin=_env(
            overrides.get("app_process_bin"), "HAL_APP_PROCESS_BIN", camera.DEFAULT_APP_PROCESS
        ),
        boot_image=_env(overrides.get("boot_image"), "HAL_BOOT_IMAGE", camera.DEFAULT_BOOT_IMAGE),
    )


def capture_frame_termux(**_overrides) -> tuple[bytes, int, int]:
    """The main-lens path: Termux:API plus ffmpeg. Hardware-verified."""
    from robot import camera

    return camera.capture_frame_termux()


def capture_frame_ffmpeg(**_overrides) -> tuple[bytes, int, int]:
    """The desktop webcam path, used when no phone camera is present."""
    from robot import camera

    return camera.capture_frame()


def auto_capture_frame(
    *,
    capture_app_process: Callable[[], tuple[bytes, int, int]] | None = None,
    capture_termux: Callable[[], tuple[bytes, int, int]] | None = None,
    capture_ffmpeg: Callable[[], tuple[bytes, int, int]] | None = None,
    termux_camera_bin: str | None = None,
) -> tuple[bytes, int, int]:
    """Pick a backend by real capability, the same way open_robot_transport
    reads TERMUX_USB_FD rather than testing for "am I on Android".

    On the phone the app_process backend is preferred because it is the only
    one that reaches the ultra-wide lens — and it degrades to the main lens
    rather than failing the turn, because a narrower picture is worth more to
    the caller than an error.
    """
    import shutil
    import sys

    from robot.camera import CameraCaptureError

    termux_camera_bin = _env(termux_camera_bin, "HAL_TERMUX_CAMERA_BIN", "termux-camera-photo")
    app_process = capture_app_process or capture_frame_app_process
    termux = capture_termux or capture_frame_termux
    ffmpeg = capture_ffmpeg or capture_frame_ffmpeg

    if shutil.which(termux_camera_bin):
        try:
            return app_process()
        except CameraCaptureError as error:
            print(
                f"[camera] wide-angle capture unavailable, falling back to the main lens: {error}",
                file=sys.stderr,
            )
            return termux()
    return ffmpeg()


def capture_visual_scene(
    capture: Callable[[], tuple[bytes, int, int]] | None = None,
    *,
    data_dir=None,
) -> tuple[dict, bytes | None]:
    """Capture one frame. Returns the tool-result dict and the raw JPEG bytes,
    or (error dict, None). Packaging the bytes for a particular transport is
    the caller's job — robot/mcp_server.py turns them into an MCP image block."""
    from robot.camera import CameraCaptureError

    if not CAMERA_ENABLED:
        return {"ok": False, "error": "camera is disabled (set HAL_ROBOT_CAMERA=1)"}, None
    try:
        image_bytes, width, height = (capture or auto_capture_frame)()
    except CameraCaptureError as error:
        return {"ok": False, "error": str(error)}, None
    except Exception as error:  # a bad capture must not kill the turn
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}, None
    name = f"capture-{int(time.time() * 1000)}.jpg"
    if data_dir is not None:
        viewscreen_dir = data_dir / "viewscreen"
        viewscreen_dir.mkdir(parents=True, exist_ok=True)
        (viewscreen_dir / name).write_bytes(image_bytes)
    return (
        {"ok": True, "path": name, "width": width, "height": height, "bytes": len(image_bytes)},
        image_bytes,
    )


# Ollama Cloud's OpenAI-compatible endpoint returns HTTP 500 for an image in a
# *tool result* message, while the identical image in a *user* message works —
# measured 2026-09-06, both directions, with a 64x64 solid-colour PNG so size
# was not a factor. Hermes builds the messages, so this app cannot move the
# frame into a user turn from inside a tool.
#
# So the frame is described where it is captured, and the tool returns prose.
# The cost is real and worth naming: the describing model sees the picture, the
# conversing model only reads about it, so anything the caption omits is gone.
# When the endpoint learns to accept tool-result images, HAL_VISION_RAW=1
# returns the bytes instead and the caption step disappears.
VISION_RAW = os.environ.get("HAL_VISION_RAW", "0").strip().lower() not in {"0", "false", "no", ""}
VISION_MODEL = os.environ.get("HAL_VISION_MODEL", "").strip()
VISION_PROMPT = os.environ.get(
    "HAL_VISION_PROMPT",
    "Describe what is in front of the camera in two short sentences. "
    "Mention obstacles, their rough direction, and anything a small wheeled "
    "robot would need to avoid. Do not speculate beyond what is visible.",
)


def _key_from_hermes_env(name: str = "OLLAMA_API_KEY") -> str:
    """Read one key out of Hermes' own env file.

    The agent loads ~/.hermes/.env itself; this app does not, so a key that is
    plainly configured looks missing from here. Read it rather than requiring
    it to be exported twice — one file remains the single place the credential
    lives, and it is never copied into this app's config.
    """
    path = Path(os.path.expanduser(os.environ.get("HAL_HERMES_ENV", "~/.hermes/.env")))
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        return ""
    return ""


def describe_frame(image_bytes: bytes, *, mime_type: str = "image/jpeg") -> dict:
    """Caption one frame with the vision model. Returns {"ok", "description"}.

    Reads the same provider config the agent uses, so there is one place the
    endpoint and key are configured rather than a second copy that can drift.
    """
    import base64
    import json as _json
    import urllib.error
    import urllib.request

    key = os.environ.get("OLLAMA_API_KEY", "").strip() or _key_from_hermes_env()
    base = os.environ.get("HAL_VISION_BASE_URL", "https://ollama.com/v1").rstrip("/")
    model = VISION_MODEL or os.environ.get("HAL_VISION_MODEL_DEFAULT", "gemma4:31b")
    if not key:
        return {"ok": False, "error": "no OLLAMA_API_KEY for the vision model"}

    payload = {
        "model": model,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"}},
        ]}],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=_json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(
                os.environ.get("HAL_VISION_TIMEOUT", "60"))) as response:
            body = _json.load(response)
    except urllib.error.HTTPError as error:
        return {"ok": False, "error": f"vision model HTTP {error.code}"}
    except Exception as error:
        return {"ok": False, "error": f"vision model unreachable: {error}"}
    try:
        return {"ok": True, "description": body["choices"][0]["message"]["content"].strip()}
    except (KeyError, IndexError):
        return {"ok": False, "error": "vision model returned no description"}


def set_board_mode(mode: str, open_transport: Callable[[], object] | None = None) -> dict:
    """Switch the CyberPi between online and upload mode.

    The frames are hardware-confirmed: a cold-boot USB capture of a real mBlock
    "Enter Live" session showed exactly ONLINE_MODE_MARKER going host-to-device,
    and the round trip (online -> upload -> online) is repeatable. See
    robot/cyberpi.py.

    Not gated by HAL_ROBOT_MOTION: this turns no motors, and it is reversible.
    Worth knowing before reaching for it — **upload mode does not block
    motion.** robot/telemetry.py records that online-exec requests were
    confirmed on real hardware to work regardless of reported mode, and that
    the online-mode requirement was this client's own assumption rather than a
    firmware precondition. So this is for matching mBlock's state, not for
    unblocking anything.

    The write is verified by reading the mode back, because the marker is a
    request rather than an acknowledgement — an unverified "ok" here would be a
    claim about hardware nobody checked.
    """
    from robot.cyberpi import UPLOAD_MODE_MARKER, encode_online_mode_frame
    from robot.telemetry import CyberPiTelemetryClient

    wanted = mode.strip().lower()
    if wanted not in {"online", "upload"}:
        return {"ok": False, "error": "mode must be 'online' or 'upload'"}

    opener = open_transport or _default_opener()
    try:
        transport = opener()
    except Exception as error:
        return {"ok": False, "error": str(error)}
    try:
        client = CyberPiTelemetryClient(transport)
        before = client.read_mode()
        frame = encode_online_mode_frame() if wanted == "online" else UPLOAD_MODE_MARKER
        transport.write(frame)
        time.sleep(float(os.environ.get("HAL_MODE_SETTLE_SECONDS", "1.0")))
        after = client.read_mode()
        after_value = getattr(after, "value", str(after))
        return {
            "ok": after_value == wanted,
            "requested": wanted,
            "mode_before": getattr(before, "value", str(before)),
            "mode_after": after_value,
        }
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    finally:
        try:
            transport.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Motion — gated
# ---------------------------------------------------------------------------

def run_motion(open_transport: Callable[[], object], act: Callable, *, limits=None) -> dict:
    """Open one transport, take a fresh telemetry sample for the interlock,
    arm, then act() immediately — the safety watchdog is 250 ms and must not
    lapse against anything slower than local Python between arming and the
    motion itself (see robot/safety.py).

    `open_transport` is a callable rather than a port string so tests can
    substitute a fake transport; robot/simulator.py is exactly that.
    """
    if not MOTION_ENABLED:
        return {"ok": False, "error": "motion is disabled (set HAL_ROBOT_MOTION=1)"}

    from robot.motion import CyberPiMotionClient
    from robot.protocol import Telemetry
    from robot.safety import MotionLimits, SafetyController
    from robot.telemetry import CyberPiTelemetryClient

    try:
        transport = open_transport()
    except Exception as error:
        return {"ok": False, "error": str(error)}
    try:
        telemetry_client = CyberPiTelemetryClient(transport)
        telemetry_client.initialize()

        safety = SafetyController(limits or MotionLimits())
        motion_client = CyberPiMotionClient(transport, safety)
        motion_client.initialize()

        # Sampled *after* the mode bootstrap, not before: initialize() costs a
        # round trip and, on a board that is not already online, sleeps 1.5 s
        # on top — so a reading taken ahead of it can be seconds old by the
        # time the proximity interlock consults it, which is the exact window
        # the interlock exists to cover.
        snapshot = telemetry_client.read_snapshot()
        telemetry = Telemetry(
            left_ticks=0,
            right_ticks=0,
            yaw_deg=snapshot.yaw_deg,
            pitch_deg=snapshot.pitch_deg,
            obstacle_dist_cm=snapshot.ultrasonic_cm,
            battery_volts=0.0,
        )
        safety.connect()
        safety.arm()
        safety.update_telemetry(telemetry)
        act(motion_client)
        return {"ok": True}
    except Exception as error:
        return {"ok": False, "error": str(error)}
    finally:
        try:
            transport.close()
        except Exception:
            pass


def drive_straight(
    open_transport: Callable[[], object] | None = None,
    distance_cm: object = 0,
    speed_pct: object = 0,
    *,
    limits=None,
) -> dict:
    opener = open_transport or _default_opener()
    return run_motion(
        opener, lambda client: client.drive_straight(distance_cm, speed_pct), limits=limits
    )


def turn(
    open_transport: Callable[[], object] | None = None,
    angle_degrees: object = 0,
    speed_pct: object = 0,
) -> dict:
    opener = open_transport or _default_opener()
    return run_motion(opener, lambda client: client.turn(angle_degrees, speed_pct))


def emergency_stop(open_transport: Callable[[], object] | None = None) -> dict:
    """Not gated by HAL_ROBOT_MOTION: stopping is always permitted, even when
    starting is not. See this module's docstring for why a stop issued while a
    drive holds the transport cannot reach the chassis — it reports the
    failure rather than claiming a stop that did not happen."""
    from robot.estop import CyberPiEmergencyStopClient

    opener = open_transport or _default_opener()
    try:
        transport = opener()
    except Exception as error:
        return {"ok": False, "error": str(error)}
    try:
        client = CyberPiEmergencyStopClient(transport)
        client.initialize()
        client.stop_all()
        return {"ok": True}
    except Exception as error:
        return {"ok": False, "error": str(error)}
    finally:
        try:
            transport.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Camera-first crawl autonomy — gated hardest of all
# ---------------------------------------------------------------------------
#
# The robot moves itself here, so the question is not "did a person approve
# this motion" but "did a person approve this *episode*". Arming therefore
# takes the same human approval a single motion does, and buys a bounded
# budget rather than one step: 25 cm total, 5 cm per segment, 10% speed, 60
# seconds, and every segment needs its own fresh camera assessment. The
# controller spends the budget even when the hardware command fails, because
# retrying after an uncertain physical outcome is worse than stopping.
#
# **The assessment is weaker here than in hal.** hal's Gemma looked at the
# frame. Ollama Cloud rejects images in tool results (see describe_frame), so
# the model assessing "clear" is reading a *description* of the picture. A
# caption that omits the table edge cannot be assessed for the table edge.
# min_vision_confidence stays at 0.9, but understand what the 0.9 is about.
#
# Crawl requires HAL_ROBOT_MOTION=1 *and* HAL_ROBOT_CAMERA=1: without sight it
# is just repeated blind driving, which is the thing the design exists to
# prevent.

_crawl_controllers: dict[str, object] = {}


def _crawl_for(session_id: str):
    from robot.crawl import CrawlController, CrawlLimits

    if session_id not in _crawl_controllers:
        _crawl_controllers[session_id] = CrawlController(CrawlLimits())
    return _crawl_controllers[session_id]


def _crawl_state(session_id: str) -> dict:
    """Status fields only — deliberately no "ok".

    These get merged into tool results, and dict union lets the right side
    win: an "ok": True in here silently overwrote a refusal's "ok": False and
    reported a rejected crawl step as a success. Status is not an outcome.
    """
    controller = _crawl_for(session_id)
    state = controller.state
    return {
        "armed": controller.is_active(),
        "remaining_cm": state.remaining_cm,
        "assessment": state.assessment,
        "assessment_confidence": state.assessment_confidence,
        "has_capture": state.capture_id is not None,
    }


def crawl_status(session_id: str) -> dict:
    """The status report as a standalone tool result."""
    return {"ok": True} | _crawl_state(session_id)


async def crawl_arm(session_id: str) -> dict:
    """Arm one crawl episode, after a human approves the whole episode."""
    if not MOTION_ENABLED:
        return {"ok": False, "error": "motion is disabled (set HAL_ROBOT_MOTION=1)"}
    if not CAMERA_ENABLED:
        return {"ok": False, "error": "crawl needs the camera (set HAL_ROBOT_CAMERA=1)"}

    from robot.crawl import CrawlLimits

    limits = CrawlLimits()
    title = (
        f"autonomous crawl: up to {limits.max_total_distance_cm} cm forward in "
        f"{limits.max_segment_cm} cm steps at {limits.max_speed_pct}% speed, "
        f"{int(limits.max_duration_seconds)}s, camera-checked each step"
    )
    request_id, future = hermes_bridge._register_permission(session_id, title)
    hermes_bridge.publish_event(session_id, {
        "type": "permission_request",
        "request_id": request_id,
        "title": title,
        "timeout": MOTION_PERMISSION_TIMEOUT,
    })
    try:
        allowed = await asyncio.wait_for(future, timeout=MOTION_PERMISSION_TIMEOUT)
    except asyncio.TimeoutError:
        allowed = False
    finally:
        hermes_bridge._pending_permissions.pop(request_id, None)
    hermes_bridge.publish_event(session_id, {
        "type": "permission_resolved",
        "request_id": request_id,
        "title": title,
        "allowed": allowed,
    })
    if not allowed:
        return {"ok": False, "error": "crawl authorization was denied or timed out"}

    _crawl_for(session_id).arm()
    return {"ok": True, "authorized": title} | _crawl_state(session_id)


def crawl_disarm(session_id: str) -> dict:
    _crawl_for(session_id).disarm()
    return {"ok": True} | _crawl_state(session_id)


def crawl_observe(session_id: str, *, data_dir=None) -> dict:
    """Capture a frame for the crawl and describe it. Records the capture so a
    following assessment can be tied to this exact frame."""
    from robot.crawl import CrawlSafetyError

    controller = _crawl_for(session_id)
    if not controller.is_active():
        return {"ok": False, "error": "crawl autonomy is not armed or has expired"}
    result, image_bytes = capture_visual_scene(data_dir=data_dir)
    if image_bytes is None:
        return result
    described = describe_frame(image_bytes)
    if not described.get("ok"):
        return {"ok": False, "error": f"cannot assess without a description: {described.get('error')}"}
    try:
        controller.record_capture(result["path"])
    except CrawlSafetyError as error:
        return {"ok": False, "error": str(error)}
    # The ultrasonic reading is reported alongside the description because the
    # base proximity interlock will check it again at transmission time, and a
    # model that has seen it will not propose a segment that is about to be
    # refused.
    sensors = read_spatial_sensors()
    return {
        "ok": True,
        "capture_id": result["path"],
        "description": described["description"],
        "ultrasonic_cm": sensors.get("ultrasonic_cm"),
        "remaining_cm": controller.state.remaining_cm,
    }


def crawl_step(session_id: str, capture_id: str, assessment: str, confidence: float,
               distance_cm: int, speed_pct: int) -> dict:
    """Record an assessment for the latest frame and, if it clears, drive one
    short segment. The budget is spent whether or not the hardware succeeds."""
    from robot.crawl import CrawlSafetyError

    controller = _crawl_for(session_id)
    try:
        controller.record_assessment(capture_id, assessment, confidence)
        controller.prepare_drive(distance_cm, speed_pct)
    except CrawlSafetyError as error:
        return {"ok": False, "error": str(error)} | _crawl_state(session_id)

    still_active = controller.consume_drive(distance_cm)
    # The drive result owns "ok" — it is the only part of this that touched
    # hardware. Status fields are merged in from _crawl_state, which has none.
    result = drive_straight(None, distance_cm, speed_pct)
    return result | {"crawl_active": still_active} | _crawl_state(session_id)


# ---------------------------------------------------------------------------
# One-use, exact-argument motion grants
# ---------------------------------------------------------------------------

# session_id -> (tool_name, canonical_arguments, expires_at_monotonic)
_motion_grants: dict[str, tuple[str, str, float]] = {}


def _canonical(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


def grant_motion_for(session_id: str, tool_name: str, canonical_arguments: str,
                     *, expires_at: float) -> None:
    _motion_grants[session_id] = (tool_name, canonical_arguments, expires_at)


def consume_motion_authorization_for(session_id: str, tool_name: str,
                                     canonical_arguments: str) -> bool:
    """One use, exact match, not expired. Popped whether or not it matches, so
    a near-miss cannot be retried against the same grant."""
    entry = _motion_grants.pop(session_id, None)
    if entry is None:
        return False
    granted_tool, granted_arguments, expires_at = entry
    if time.monotonic() > expires_at:
        return False
    return granted_tool == tool_name and granted_arguments == canonical_arguments


def check_and_consume_motion_grant(session_id: str, name: str, arguments: dict) -> dict | None:
    """`None` when the call is authorized; a refusal dict otherwise. This is
    the single check standing between a tool call and the motors."""
    if not consume_motion_authorization_for(session_id, name, _canonical(arguments)):
        return {"ok": False, "error": "motion is not authorized for this session"}
    return None


def authorized_motion_request(arguments: dict) -> tuple[str, dict, str] | None:
    """Validate and describe the exact action a person is being asked to approve.

    Bounds are enforced here as well as in robot/safety.py: this is what the
    approval prompt *says*, and a prompt that describes a motion the limits
    would later clamp would be a lie to the person approving it.
    """
    motion = arguments.get("motion")
    speed = arguments.get("speed_pct")
    if isinstance(speed, bool) or not isinstance(speed, int) or not 1 <= speed <= 30:
        return None
    if motion == "drive_straight":
        distance = arguments.get("distance_cm")
        if isinstance(distance, bool) or not isinstance(distance, int) or not -50 <= distance <= 50:
            return None
        direction = "forward" if distance >= 0 else "backward"
        return (
            motion,
            {"distance_cm": distance, "speed_pct": speed},
            f"drive {direction} {abs(distance)} cm at {speed}% speed",
        )
    if motion == "turn":
        angle = arguments.get("angle_degrees")
        if isinstance(angle, bool) or not isinstance(angle, int) or not -180 <= angle <= 180:
            return None
        direction = "right" if angle >= 0 else "left"
        return (
            motion,
            {"angle_degrees": angle, "speed_pct": speed},
            f"turn {direction} {abs(angle)} degrees at {speed}% speed",
        )
    return None


async def request_motion_authorization(session_id: str, arguments: dict, *,
                                       call_id: str = "") -> dict:
    """Prompt the person at the interface and mint a one-use grant on approval."""
    if not MOTION_ENABLED:
        return {"ok": False, "error": "motion is disabled (set HAL_ROBOT_MOTION=1)"}

    parsed = authorized_motion_request(arguments)
    if parsed is None:
        return {"ok": False, "error": "invalid motion authorization request"}
    tool_name, motion_arguments, title = parsed

    request_id, future = hermes_bridge._register_permission(session_id, title)
    hermes_bridge.publish_event(session_id, {
        "type": "permission_request",
        "request_id": request_id,
        "tool_call_id": call_id,
        "title": title,
        "timeout": MOTION_PERMISSION_TIMEOUT,
    })
    try:
        allowed = await asyncio.wait_for(future, timeout=MOTION_PERMISSION_TIMEOUT)
    except asyncio.TimeoutError:
        allowed = False
    finally:
        hermes_bridge._pending_permissions.pop(request_id, None)
    hermes_bridge.publish_event(session_id, {
        "type": "permission_resolved",
        "request_id": request_id,
        "title": title,
        "allowed": allowed,
    })
    if not allowed:
        return {"ok": False, "error": "motion authorization was denied or timed out"}
    grant_motion_for(
        session_id,
        tool_name,
        _canonical(motion_arguments),
        expires_at=time.monotonic() + MOTION_PERMISSION_TIMEOUT,
    )
    return {"ok": True, "authorized": title}
