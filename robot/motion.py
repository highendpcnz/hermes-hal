"""The one real-motion CyberPi client: bounded, self-terminating drive and turn.

Every command here maps 1:1 to a verified mBot2 Python API primitive — confirmed
against Makeblock's own mBot2 Python curriculum booklet, not guessed:
`cyberpi.mbot2.straight(distance_cm, speed=...)` and `cyberpi.mbot2.turn(degrees,
speed=...)`. Those are chosen deliberately over `forward()`/`backward()` (which
run *forever* with no `run_time`, or race a manually-supplied timer against the
real move) and `turn_left()`/`turn_right()` (time-bounded, same race). `straight()`
and `turn()` are bounded by the quantity that actually matters — distance,
degrees — and self-terminate on the CyberPi firmware side without the host
needing to time anything.

Every request is validated by the same `SafetyController` bounds the simulator
uses (`prepare_drive`/`prepare_turn` in safety.py) before it is ever encoded, so
a real command can never exceed the bench limits the simulator already enforces.

Open question, not yet resolved: once a `straight()`/`turn()` script is sent and
this client is blocked waiting for its response, it is unknown whether CyberPi's
online executor can run a second script (e.g. a stop) concurrently, queues it
until the first finishes, or rejects it — this has not been tested. Until it is,
treat every command here as uninterruptible once sent, and rely on small bounded
distances/angles/speeds — not a mid-flight stop — as the real safety margin. See
docs/robot-control-contract.md "Movement".
"""

from __future__ import annotations

import time
from typing import Callable

from .cyberpi import (
    BAUD_RATE,
    ONLINE_PROTOCOL_ID,
    ONLINE_WAIT,
    CyberPiFrame,
    CyberPiMode,
    CyberPiProtocolError,
    F3F4FrameDecoder,
    decode_current_mode_response,
    decode_online_response_payload,
    encode_current_mode_query_frame,
    encode_online_mode_frame,
    encode_online_request_frame,
)
from .safety import SafetyController
from .telemetry import (
    CyberPiClientError,
    CyberPiNotReadyError,
    CyberPiRemoteError,
    CyberPiTimeoutError,
    SerialTransport,
)

# Real motion commands run for as long as the physical move takes, which a
# telemetry-query timeout (1.5s) is far too short for; this must comfortably
# exceed the slowest bounded command MotionLimits can produce.
DEFAULT_MOTION_TIMEOUT_SECONDS = 8.0

# Empirically observed settle time after sending ONLINE_MODE_MARKER before the
# device reliably reports online mode on a re-query — two live round trips on
# the Mac (see docs/termux-usb-bringup.md) both confirmed within this window;
# not documented or guaranteed by CyberPi's firmware.
MODE_SWITCH_SETTLE_SECONDS = 1.5


class CyberPiMotionClient:
    """Verify online mode, then allow exactly two commands: straight-line drive
    and turn-in-place, both bounded and both validated against a SafetyController
    before anything is sent."""

    def __init__(
        self,
        transport: SerialTransport,
        safety: SafetyController,
        *,
        timeout_seconds: float = DEFAULT_MOTION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise CyberPiProtocolError("timeout_seconds must be positive")
        self.transport = transport
        self.safety = safety
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._decoder = F3F4FrameDecoder()
        self._pending: list[CyberPiFrame] = []
        self._sequence = 0
        self._initialized = False
        self._closed = False

    @classmethod
    def open(
        cls,
        port: str,
        safety: SafetyController,
        *,
        timeout_seconds: float = DEFAULT_MOTION_TIMEOUT_SECONDS,
    ) -> "CyberPiMotionClient":
        """Open a pyserial connection without making pyserial an import-time dependency."""

        try:
            import serial
        except ImportError as error:
            raise CyberPiClientError(
                "pyserial is required for hardware access; install requirements.txt"
            ) from error
        transport = serial.Serial(port, BAUD_RATE, timeout=0.05, write_timeout=1.0)
        transport.reset_input_buffer()
        return cls(transport, safety, timeout_seconds=timeout_seconds)

    def __enter__(self) -> "CyberPiMotionClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self.transport.close()
            self._closed = True
            self._initialized = False

    def initialize(self) -> CyberPiMode:
        """Read mode and require online. Both motion methods refuse to run
        before this succeeds.

        Mode IS enforced here, unlike telemetry.py's read-only `initialize()`.
        A real `drive_straight()` was hardware-confirmed to be silently
        accepted and acknowledged by the online-exec channel while in upload
        mode, but to produce no physical actuation at all — no error, no
        signal, just nothing at the wheels (docs/termux-usb-bringup.md). That
        silent-no-op failure mode is worse than an exception, so this refuses
        to arm outside genuine online mode rather than merely logging it.

        If the board isn't online yet, this sends ONLINE_MODE_MARKER — the
        real mode-switch command, hardware-confirmed via a full USB capture
        of a real mBlock session plus a direct live round-trip test (see
        cyberpi.ONLINE_MODE_MARKER's docstring and
        docs/termux-usb-bringup.md). An earlier version of this bootstrap
        tried to trigger the switch by running mBlock's `online_restart`
        script instead; that was a misread of the capture — the script
        reference was a bare name lookup, not a call, and hardware-confirmed
        to do nothing to the reported mode either way.
        """

        mode = self._query_mode()
        if mode is not CyberPiMode.ONLINE:
            self._write(encode_online_mode_frame())
            self._sleeper(MODE_SWITCH_SETTLE_SECONDS)
            mode = self._query_mode()
        if mode is not CyberPiMode.ONLINE:
            raise CyberPiNotReadyError(f"CyberPi reports mode={mode.value!r}, not online")
        self._initialized = True
        return mode

    def _query_mode(self) -> CyberPiMode:
        self._write(encode_current_mode_query_frame())
        frame = self._read_matching(
            lambda item: len(item.payload) == 3 and item.payload[:2] == bytes((0x0D, 0x80)),
            "current-mode response",
        )
        return decode_current_mode_response(frame.payload)

    def drive_straight(self, distance_cm: int, speed_pct: int, now: float | None = None) -> None:
        """cyberpi.mbot2.straight(distance_cm, speed=speed_pct). Positive distance
        is forward, negative is backward — bounded and validated by SafetyController
        before anything is sent."""

        self.safety.prepare_drive(distance_cm, speed_pct, now)
        self._run(f"cyberpi.mbot2.straight({int(distance_cm)}, speed = {int(speed_pct)})")

    def turn(self, angle_degrees: int, speed_pct: int, now: float | None = None) -> None:
        """cyberpi.mbot2.turn(angle_degrees, speed=speed_pct) — bounded and
        validated by SafetyController before anything is sent."""

        self.safety.prepare_turn(angle_degrees, speed_pct, now)
        self._run(f"cyberpi.mbot2.turn({int(angle_degrees)}, speed = {int(speed_pct)})")

    def _run(self, script: str) -> None:
        if not self._initialized:
            raise CyberPiNotReadyError("initialize() must succeed before sending motion")
        sequence = self._next_sequence()
        self._write(encode_online_request_frame(script, sequence=sequence, wait_for_response=True))
        frame = self._read_matching(
            lambda item: _is_online_response(item, sequence),
            f"online response for sequence {sequence}",
        )
        response = decode_online_response_payload(frame.payload)
        if response.error is not None:
            raise CyberPiRemoteError(response.error, script)

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return sequence

    def _write(self, data: bytes) -> None:
        if self._closed:
            raise CyberPiNotReadyError("CyberPi serial client is closed")
        written = self.transport.write(data)
        if written != len(data):
            raise CyberPiClientError(f"serial write accepted {written} of {len(data)} bytes")
        self.transport.flush()

    def _read_matching(
        self,
        predicate: Callable[[CyberPiFrame], bool],
        description: str,
    ) -> CyberPiFrame:
        for index, frame in enumerate(self._pending):
            if predicate(frame):
                return self._pending.pop(index)

        deadline = self._clock() + self.timeout_seconds
        while self._clock() < deadline:
            chunk = self.transport.read(max(1, self.transport.in_waiting))
            if chunk:
                for frame in self._decoder.feed(chunk):
                    if predicate(frame):
                        return frame
                    self._pending.append(frame)
                if len(self._pending) > 64:
                    self._pending = self._pending[-64:]
                continue
            remaining = deadline - self._clock()
            if remaining > 0:
                self._sleeper(min(0.005, remaining))
        raise CyberPiTimeoutError(f"timed out waiting for {description}")


def _is_online_response(frame: CyberPiFrame, sequence: int) -> bool:
    payload = frame.payload
    return (
        len(payload) >= 6
        and payload[:2] == bytes((ONLINE_PROTOCOL_ID, ONLINE_WAIT))
        and int.from_bytes(payload[2:4], "little") == sequence
    )
