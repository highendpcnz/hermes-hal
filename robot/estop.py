"""The one motor-capable CyberPi client in this codebase: verify online mode, then
send exactly one command — `cyberpi.mbot2.EM_stop(port = "all")` — and nothing else.

`CyberPiTelemetryClient` (telemetry.py) is read-only by construction; this class is
deliberately kept separate from it rather than gaining one more method, so "read-only"
stays a guarantee enforced by which class you imported, not a convention someone can
drift away from later. `EM_stop` is the mBot2 API's documented all-motors stop (see
docs/robot-control-contract.md); this client cannot drive, turn, or set motor power —
there is no method here that could.
"""

from __future__ import annotations

import argparse
import sys
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
from .telemetry import (
    CyberPiClientError,
    CyberPiNotReadyError,
    CyberPiRemoteError,
    CyberPiTimeoutError,
    SerialTransport,
)

STOP_ALL_SCRIPT = 'cyberpi.mbot2.EM_stop(port = "all")'

# Empirically observed settle time after sending ONLINE_MODE_MARKER before the
# device reliably reports online mode on a re-query — two live round trips on
# the Mac (see docs/termux-usb-bringup.md) both confirmed within this window;
# not documented or guaranteed by CyberPi's firmware.
MODE_SWITCH_SETTLE_SECONDS = 1.5


class CyberPiEmergencyStopClient:
    """Verify online mode, then allow exactly one command: stop every motor."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        timeout_seconds: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise CyberPiProtocolError("timeout_seconds must be positive")
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._decoder = F3F4FrameDecoder()
        self._pending: list[CyberPiFrame] = []
        self._sequence = 0
        self._initialized = False
        self._closed = False

    @classmethod
    def open(cls, port: str, *, timeout_seconds: float = 1.5) -> "CyberPiEmergencyStopClient":
        """Open a pyserial connection without making pyserial an import-time dependency."""

        try:
            import serial
        except ImportError as error:
            raise CyberPiClientError(
                "pyserial is required for hardware access; install requirements.txt"
            ) from error
        transport = serial.Serial(port, BAUD_RATE, timeout=0.05, write_timeout=1.0)
        transport.reset_input_buffer()
        return cls(transport, timeout_seconds=timeout_seconds)

    def __enter__(self) -> "CyberPiEmergencyStopClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self.transport.close()
            self._closed = True
            self._initialized = False

    def initialize(self) -> CyberPiMode:
        """Read mode and require online; `stop_all` refuses to run before this
        succeeds.

        Mode IS enforced here, unlike telemetry.py's read-only `initialize()`.
        A real motor command was hardware-confirmed to be silently accepted
        and acknowledged by the online-exec channel while in upload mode, but
        to produce no physical actuation at all — no error, no signal, just
        nothing at the wheels (docs/termux-usb-bringup.md). For a client whose
        entire purpose is stopping the robot, a silent no-op is the one
        failure mode that cannot be tolerated, so this refuses to arm outside
        genuine online mode rather than merely logging it.

        If the board isn't online yet, this sends ONLINE_MODE_MARKER —
        the real mode-switch command, hardware-confirmed via a full USB
        capture of a real mBlock session plus a direct live round-trip test
        (see cyberpi.ONLINE_MODE_MARKER's docstring and
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

    def stop_all(self) -> None:
        """Send EM_stop(port="all"). Raises if CyberPi reports an execution error."""

        if not self._initialized:
            raise CyberPiNotReadyError("initialize() must succeed before stop_all()")
        sequence = self._next_sequence()
        self._write(
            encode_online_request_frame(STOP_ALL_SCRIPT, sequence=sequence, wait_for_response=True)
        )
        frame = self._read_matching(
            lambda item: _is_online_response(item, sequence),
            f"online response for sequence {sequence}",
        )
        response = decode_online_response_payload(frame.payload)
        if response.error is not None:
            raise CyberPiRemoteError(response.error, STOP_ALL_SCRIPT)

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send CyberPi/mBot2 the only motor command in this codebase: stop all motors"
    )
    parser.add_argument("--port", default="/dev/cu.usbserial-110")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="required acknowledgement that this sends a real command to the robot",
    )
    args = parser.parse_args(argv)

    try:
        with CyberPiEmergencyStopClient.open(args.port, timeout_seconds=args.timeout) as client:
            mode = client.initialize()
            print(f"online mode confirmed ({mode.value}); sending EM_stop(port='all')")
            client.stop_all()
            print("stop_all: ok")
    except (CyberPiClientError, CyberPiProtocolError, OSError) as error:
        print(f"stop_all failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
