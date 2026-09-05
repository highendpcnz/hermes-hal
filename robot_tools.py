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

**Motion is disabled unless HAL_ROBOT_MOTION=1.** Sensors, telemetry and the
camera are read-only and always available when hardware is present; anything
that turns a motor is off by default. That is not timidity about an unfinished
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
        client.initialize()
        snapshot = client.read_snapshot()
        return {
            "ok": True,
            "ultrasonic_cm": snapshot.ultrasonic_cm,
            "yaw_deg": snapshot.yaw_deg,
            "pitch_deg": snapshot.pitch_deg,
        }
    except Exception as error:
        return {"ok": False, "error": str(error)}
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
