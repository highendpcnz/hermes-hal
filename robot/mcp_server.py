"""Stdio MCP server exposing Hermes Hal's robot tools to Hermes Agent.

Adapted from hal's `robot/mcp_server.py` at commit 1318bd1 (which exposed the
same shape to Codex CLI) — see docs/technology-transfer.md.

Spawned by Hermes Agent itself, as a separate process, so it has no access to
this app's in-memory motion grants or the CyberPi's transport. Every tool below
is a thin HTTP proxy to the bridge's own `/internal/robot/*` routes, which do
the real work through `robot_tools.py`. There is therefore exactly one
implementation of the safety-relevant logic, and it is not this file.

`session_token` is an opaque token the model echoes back rather than a trusted
argument: the bridge resolves it to a browser session and refuses if it does
not know it. A model that invents one gets a 403, not a moving robot.

Register it on the phone with:

    hermes mcp add hal-robot -- python3 -m robot.mcp_server

Manual smoke test against a running bridge:

    HAL_BRIDGE_URL=http://127.0.0.1:8000 python3 -m robot.mcp_server
"""

from __future__ import annotations

import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.mcpserver import Image, MCPServer

BRIDGE_URL = os.environ.get("HAL_BRIDGE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT = float(os.environ.get("HAL_ROBOT_TOOL_TIMEOUT", "30"))
# /internal/robot/authorize and /internal/robot/crawl/arm block server-side for
# up to robot_tools.MOTION_PERMISSION_TIMEOUT (5 minutes by default) waiting on
# a human's Allow/Deny — that is the whole point of the call, not a hang. A
# client-side timeout shorter than the server's own wait means the MCP call
# always dies before a person could ever approve it, which is exactly what
# happened live (confirmed 2026-09-06: crawl_arm timed out twice at ~30.1s
# each, Hermes gave up and fell back to blind exploration instead). Padded a
# few seconds past the server's wait so a genuine server-side timeout is what
# actually fires, not a race between the two.
ARM_TIMEOUT = float(os.environ.get("HAL_ROBOT_ARM_TIMEOUT", "310"))

server = MCPServer("hal-robot")


def _post(path: str, payload: dict, *, timeout: float = TIMEOUT) -> dict:
    request = Request(
        f"{BRIDGE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        try:
            return json.loads(error.read())
        except (json.JSONDecodeError, OSError):
            return {"ok": False, "error": f"Hermes Hal bridge HTTP {error.code}"}
    except URLError as error:
        return {"ok": False, "error": f"cannot reach the Hermes Hal bridge: {error.reason}"}


@server.tool()
def read_spatial_sensors(session_token: str) -> dict:
    """Read the robot's ultrasonic distance and orientation. Read-only."""
    return _post("/internal/robot/sensors", {"session_token": session_token})


@server.tool()
def request_motion_authorization(
    session_token: str, motion: str, speed_pct: int,
    distance_cm: int = 0, angle_degrees: int = 0,
) -> dict:
    """Ask the operator to approve ONE exact motion. This does not move the
    robot. `motion` is "drive_straight" (uses distance_cm, -50..50) or "turn"
    (uses angle_degrees, -180..180); speed_pct is 1..30. Approval mints a
    single-use grant bound to these exact arguments — call `move` with the
    same values or it will be refused."""
    return _post("/internal/robot/authorize", {
        "session_token": session_token,
        "arguments": {
            "motion": motion, "speed_pct": speed_pct,
            "distance_cm": distance_cm, "angle_degrees": angle_degrees,
        },
    }, timeout=ARM_TIMEOUT)


@server.tool()
def move(
    session_token: str, motion: str, speed_pct: int,
    distance_cm: int = 0, angle_degrees: int = 0,
) -> dict:
    """Execute a motion that was already approved by
    `request_motion_authorization`, with the SAME arguments. Refused
    otherwise. Motion is bounded and self-terminates on the firmware side; it
    cannot be interrupted once started."""
    return _post("/internal/robot/move", {
        "session_token": session_token,
        "arguments": {
            "motion": motion, "speed_pct": speed_pct,
            "distance_cm": distance_cm, "angle_degrees": angle_degrees,
        },
    })


@server.tool()
def look(session_token: str):
    """Take one photograph through the robot's camera and return it, so you can
    see what is in front of the robot. Use this when asked what you can see, or
    when a decision depends on the scene rather than on a distance reading.
    Prefers the ultra-wide lens (about 104 degrees) and falls back to the main
    lens. Returns an error string if the camera is disabled."""
    result = _post("/internal/robot/look", {"session_token": session_token})
    if not result.get("ok"):
        return result.get("error", "camera unavailable")
    encoded = result.pop("image_base64", None)
    if encoded:
        # Raw path — only reachable with HAL_VISION_RAW=1, because Ollama
        # Cloud returns HTTP 500 for an image in a tool result.
        return [
            f"Camera frame: {result.get('width')}x{result.get('height')}, "
            f"{result.get('bytes')} bytes, saved as {result.get('path')}.",
            Image(data=base64.b64decode(encoded), format="jpeg"),
        ]
    if result.get("description"):
        return (
            f"Looking through the camera ({result.get('width')}x{result.get('height')}, "
            f"saved as {result.get('path')}), I can see: {result['description']}"
        )
    return (
        f"A frame was captured ({result.get('path')}) but it could not be described: "
        f"{result.get('description_error', 'unknown')}"
    )


@server.tool()
def crawl_arm(session_token: str) -> dict:
    """Ask the operator to authorise ONE autonomous crawl episode. This does not
    move the robot. On approval you may repeat crawl_observe then crawl_step
    until the budget runs out: 200 cm total, 15 cm per step, 10% speed, 300
    seconds. Every step needs its own fresh camera assessment. This call can
    legitimately take several minutes to return — it is waiting on a person,
    not hung."""
    return _post("/internal/robot/crawl/arm", {"session_token": session_token}, timeout=ARM_TIMEOUT)


@server.tool()
def crawl_status(session_token: str) -> dict:
    """Whether a crawl episode is armed, and how much budget is left."""
    return _post("/internal/robot/crawl/status", {"session_token": session_token})


@server.tool()
def crawl_disarm(session_token: str) -> dict:
    """End the crawl episode immediately. Always available."""
    return _post("/internal/robot/crawl/disarm", {"session_token": session_token})


@server.tool()
def crawl_observe(session_token: str) -> dict:
    """Take a fresh photograph for the crawl and get it described, plus the
    current ultrasonic distance. Returns a capture_id you must pass to
    crawl_step — an assessment of an older frame will be refused.

    Judge one narrow question: is the floor this robot is about to cross clear?
    That is roughly 15 cm of ground, and the description is written to answer
    exactly that. `ultrasonic_cm` is the distance to the nearest thing straight
    ahead, so a large reading means nothing in the picture can reach this step
    however much furniture the description names. A chair across the room is
    not a reason to say blocked — the next step gets its own photograph.

    Blocked means something the robot would hit, or an edge it would fall off:
    an object lying in its path, a cable, a step down, a small ultrasonic
    reading. A flat floor covering — a rug, a mat, a threshold strip — is
    drivable ground, not an obstacle. Also say "blocked", or use a low
    confidence, when the description is too vague to tell what is on the near
    floor: vagueness about the ground immediately ahead is a real reason to
    stop, where distant scenery is not."""
    return _post("/internal/robot/crawl/observe", {"session_token": session_token})


@server.tool()
def crawl_step(
    session_token: str, capture_id: str, assessment: str, confidence: float,
    distance_cm: int, speed_pct: int,
) -> dict:
    """Record your assessment of the latest capture and, if it is "clear" with
    confidence of at least 0.9, drive one short segment forward. assessment is
    "clear", "blocked" or "unknown"; distance_cm is 1..15; speed_pct is 1..10.
    The budget is spent whether or not the motion succeeds, so do not retry a
    segment whose outcome you are unsure of."""
    return _post("/internal/robot/crawl/step", {
        "session_token": session_token,
        "arguments": {
            "capture_id": capture_id, "assessment": assessment,
            "confidence": confidence, "distance_cm": distance_cm, "speed_pct": speed_pct,
        },
    })


@server.tool()
def emergency_stop(session_token: str) -> dict:
    """Stop all motors. Always permitted, even when motion is disabled. Note
    that a stop issued while a drive is already running cannot reach the
    chassis — the drive holds the serial transport — and this will say so
    rather than report a stop that did not happen."""
    return _post("/internal/robot/stop", {"session_token": session_token})


if __name__ == "__main__":
    server.run()
