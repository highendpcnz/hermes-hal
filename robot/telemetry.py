"""Read-only CyberPi serial session and validated mBot2 telemetry snapshots."""

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
import sys
import time
from typing import Callable, Protocol

from .cyberpi import (
    BAUD_RATE,
    ONLINE_PROTOCOL_ID,
    ONLINE_WAIT,
    CyberPiFrame,
    CyberPiMode,
    CyberPiProtocolError,
    MAX_ONLINE_SCRIPT_BYTES,
    F3F4FrameDecoder,
    decode_current_mode_response,
    decode_firmware_version_response,
    decode_online_response_payload,
    encode_current_mode_query_frame,
    encode_firmware_version_query_frame,
    encode_online_request_frame,
)


# A snapshot used to cost six round trips, one per query. It now costs two.
#
# Measured on the real chassis (robot/bench_telemetry.py, firmware 44.01.011):
# a round trip is ~60ms of fixed overhead plus the getters' own work, so
# issuing six of them spent 706ms where two spend 442ms -- 115ms for the five
# sensor getters that used to cost 213ms, and 327ms for the six encoder
# getters that used to cost 494ms. These are isolated hardware measurements;
# the later 2-3s /api/say samples skipped the sensor tool altogether and cannot
# establish an end-to-end saving. See docs/sensor-benchmark-handoff.md.
#
# All eleven values in ONE script would be better still and is not possible:
# the board caps a script at MAX_ONLINE_SCRIPT_BYTES (249) and the combined
# form is 310. The split below is the largest batching the wire allows, and
# the assertion under it fails the import rather than the robot if anyone
# lengthens a script past the cap.
#
# Set HAL_TELEMETRY_BATCH=0 to go back to one query per value -- an escape
# hatch for a firmware that dislikes long getter lists, not something any
# measurement here suggests is needed.
_ATTITUDE_SCRIPT = "[cyberpi.get_pitch(),cyberpi.get_roll(),cyberpi.get_yaw()]"
_ENCODER_SPEED_SCRIPT = (
    '[cyberpi.mbot2.EM_get_speed("EM1"),cyberpi.mbot2.EM_get_speed("EM2")]'
)
_ENCODER_POWER_SCRIPT = (
    '[cyberpi.mbot2.EM_get_power("EM1"),cyberpi.mbot2.EM_get_power("EM2")]'
)
_ENCODER_ANGLE_SCRIPT = (
    '[cyberpi.mbot2.EM_get_angle("EM1"),cyberpi.mbot2.EM_get_angle("EM2")]'
)

# The two batched forms. `{index}` is the configured ultrasonic port.
_SENSOR_BATCH_TEMPLATE = (
    "[mbuild.ultrasonic2.get({index}),cyberpi.get_battery(),"
    "cyberpi.get_pitch(),cyberpi.get_roll(),cyberpi.get_yaw()]"
)
_ENCODER_BATCH_SCRIPT = (
    '[cyberpi.mbot2.EM_get_speed("EM1"),cyberpi.mbot2.EM_get_speed("EM2"),'
    'cyberpi.mbot2.EM_get_power("EM1"),cyberpi.mbot2.EM_get_power("EM2"),'
    'cyberpi.mbot2.EM_get_angle("EM1"),cyberpi.mbot2.EM_get_angle("EM2")]'
)
BATCH_TELEMETRY = os.environ.get("HAL_TELEMETRY_BATCH", "1").strip() not in {
    "0",
    "false",
    "no",
}

for _label, _script in (
    ("sensor batch", _SENSOR_BATCH_TEMPLATE.format(index=8)),  # widest index
    ("encoder batch", _ENCODER_BATCH_SCRIPT),
):
    if len(_script.encode()) > MAX_ONLINE_SCRIPT_BYTES:
        raise CyberPiProtocolError(
            f"{_label} is {len(_script.encode())} bytes, over the "
            f"{MAX_ONLINE_SCRIPT_BYTES}-byte online-script cap"
        )


class SerialTransport(Protocol):
    """The small pyserial-compatible surface used by the telemetry client."""

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


class CyberPiClientError(RuntimeError):
    """Base class for telemetry-session failures outside frame decoding."""


class CyberPiTimeoutError(CyberPiClientError):
    """Raised when CyberPi does not return a matching frame in time."""


class CyberPiRemoteError(CyberPiClientError):
    """Raised when CyberPi returns a structured online-execution error."""

    def __init__(self, error: str, script: str) -> None:
        self.error = error
        self.script = script
        super().__init__(f"CyberPi returned {error} for read-only query {script!r}")


class CyberPiNotReadyError(CyberPiClientError):
    """Raised when telemetry is requested before read-only bring-up succeeds."""


@dataclass(frozen=True, slots=True)
class CyberPiBringUp:
    """Identity and sensor result required before telemetry polling."""

    mode: CyberPiMode
    firmware_version: str
    ultrasonic_index: int
    ultrasonic_cm: float


@dataclass(frozen=True, slots=True)
class CyberPiTelemetrySnapshot:
    """One getter-only snapshot from the connected CyberPi and mBot2 chassis."""

    monotonic_time: float
    battery_percent: float
    ultrasonic_cm: float
    pitch_deg: float
    roll_deg: float
    yaw_deg: float
    left_speed_rpm: float
    right_speed_rpm: float
    left_power_percent: float
    right_power_percent: float
    left_angle_deg: float
    right_angle_deg: float

    @property
    def motors_stationary(self) -> bool:
        return (
            abs(self.left_speed_rpm) < 0.01
            and abs(self.right_speed_rpm) < 0.01
            and abs(self.left_power_percent) < 0.01
            and abs(self.right_power_percent) < 0.01
        )

    @property
    def ultrasonic_out_of_range(self) -> bool:
        return self.ultrasonic_cm >= 300.0


class CyberPiTelemetryClient:
    """Sequence-matched, read-only CyberPi client with no motor methods."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        ultrasonic_index: int = 1,
        timeout_seconds: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(ultrasonic_index, bool) or not 1 <= ultrasonic_index <= 8:
            raise CyberPiProtocolError("ultrasonic_index must be an integer from 1 to 8")
        if timeout_seconds <= 0:
            raise CyberPiProtocolError("timeout_seconds must be positive")
        self.transport = transport
        self.ultrasonic_index = ultrasonic_index
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
        *,
        ultrasonic_index: int = 1,
        timeout_seconds: float = 1.5,
    ) -> "CyberPiTelemetryClient":
        """Open a pyserial connection without making pyserial an import-time dependency."""

        try:
            import serial
        except ImportError as error:
            raise CyberPiClientError(
                "pyserial is required for hardware access; install requirements.txt"
            ) from error
        transport = serial.Serial(
            port,
            BAUD_RATE,
            timeout=0.05,
            write_timeout=1.0,
        )
        transport.reset_input_buffer()
        return cls(
            transport,
            ultrasonic_index=ultrasonic_index,
            timeout_seconds=timeout_seconds,
        )

    def __enter__(self) -> "CyberPiTelemetryClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self.transport.close()
            self._closed = True
            self._initialized = False

    def initialize(self) -> CyberPiBringUp:
        """Read mode and firmware, then prime the ultrasonic module directly.

        Mode is logged, not enforced: online-exec requests were confirmed to
        work regardless of the reported mode (real hardware test, Termux/USB
        bring-up — see docs/termux-usb-bringup.md) — the online-mode
        requirement here was our own client's assumption, not a firmware
        precondition, so a non-online mode no longer blocks bring-up.
        """

        mode = self.read_mode()
        if mode is not CyberPiMode.ONLINE:
            print(f"[cyberpi] mode={mode.value} (not online, proceeding anyway)")
        firmware_version = self.read_firmware_version()
        ultrasonic_cm = self.read_ultrasonic()
        self._initialized = True
        return CyberPiBringUp(
            mode=mode,
            firmware_version=firmware_version,
            ultrasonic_index=self.ultrasonic_index,
            ultrasonic_cm=ultrasonic_cm,
        )

    def read_mode(self) -> CyberPiMode:
        self._write(encode_current_mode_query_frame())
        frame = self._read_matching(
            lambda item: len(item.payload) == 3 and item.payload[:2] == bytes((0x0D, 0x80)),
            "current-mode response",
        )
        return decode_current_mode_response(frame.payload)

    def read_firmware_version(self) -> str:
        self._write(encode_firmware_version_query_frame())
        frame = self._read_matching(
            lambda item: len(item.payload) == 10 and item.payload[0] == 0x06,
            "firmware-version response",
        )
        return decode_firmware_version_response(frame.payload)

    def read_ultrasonic(self) -> float:
        value = self._query_value(f"mbuild.ultrasonic2.get({self.ultrasonic_index})")
        distance = _number(value, "ultrasonic distance")
        if not 0 < distance <= 300:
            raise CyberPiProtocolError(
                f"ultrasonic index {self.ultrasonic_index} returned invalid distance {distance}"
            )
        return distance

    def read_snapshot(self, *, batch: bool | None = None) -> CyberPiTelemetrySnapshot:
        """Read a validated snapshot; an explicit batch choice supports paired benchmarks."""
        if not self._initialized:
            raise CyberPiNotReadyError("initialize() must succeed before reading telemetry")

        use_batch = BATCH_TELEMETRY if batch is None else batch
        if use_batch:
            ultrasonic_cm, battery_percent, pitch, roll, yaw = _number_list(
                self._query_value(
                    _SENSOR_BATCH_TEMPLATE.format(index=self.ultrasonic_index)
                ),
                5,
                "sensor batch",
            )
            left_speed, right_speed, left_power, right_power, left_angle, right_angle = (
                _number_list(self._query_value(_ENCODER_BATCH_SCRIPT), 6, "encoder batch")
            )
            # read_ultrasonic() applies this bound on the single-query path; the
            # batch must not be the way an invalid distance gets in.
            if not 0 < ultrasonic_cm <= 300:
                raise CyberPiProtocolError(
                    f"ultrasonic index {self.ultrasonic_index} returned invalid "
                    f"distance {ultrasonic_cm}"
                )
        else:
            ultrasonic_cm = self.read_ultrasonic()
            battery_percent = _number(self._query_value("cyberpi.get_battery()"), "battery")
            pitch, roll, yaw = _number_list(
                self._query_value(_ATTITUDE_SCRIPT), 3, "attitude"
            )
            left_speed, right_speed = _number_list(
                self._query_value(_ENCODER_SPEED_SCRIPT), 2, "encoder speed"
            )
            left_power, right_power = _number_list(
                self._query_value(_ENCODER_POWER_SCRIPT), 2, "encoder power"
            )
            left_angle, right_angle = _number_list(
                self._query_value(_ENCODER_ANGLE_SCRIPT), 2, "encoder angle"
            )
        if not 0 <= battery_percent <= 100:
            raise CyberPiProtocolError(f"battery percentage is outside 0..100: {battery_percent}")
        return CyberPiTelemetrySnapshot(
            monotonic_time=self._clock(),
            battery_percent=battery_percent,
            ultrasonic_cm=ultrasonic_cm,
            pitch_deg=pitch,
            roll_deg=roll,
            yaw_deg=yaw,
            left_speed_rpm=left_speed,
            right_speed_rpm=right_speed,
            left_power_percent=left_power,
            right_power_percent=right_power,
            left_angle_deg=left_angle,
            right_angle_deg=right_angle,
        )

    def _query_value(self, script: str) -> object:
        sequence = self._next_sequence()
        self._write(
            encode_online_request_frame(script, sequence=sequence, wait_for_response=True)
        )
        frame = self._read_matching(
            lambda item: _is_online_response(item, sequence),
            f"online response for sequence {sequence}",
        )
        response = decode_online_response_payload(frame.payload)
        if response.error is not None:
            raise CyberPiRemoteError(response.error, script)
        return response.result

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


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CyberPiProtocolError(f"{label} is not numeric: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise CyberPiProtocolError(f"{label} is not finite: {number}")
    return number


def _number_list(value: object, size: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise CyberPiProtocolError(f"{label} must contain {size} values: {value!r}")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read CyberPi/mBot2 telemetry without motion")
    parser.add_argument("--port", default="/dev/cu.usbserial-110")
    parser.add_argument("--ultrasonic-index", type=int, default=1)
    parser.add_argument("--samples", type=_positive_int, default=1)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=1.5)
    args = parser.parse_args(argv)

    if args.interval < 0:
        parser.error("--interval cannot be negative")
    try:
        with CyberPiTelemetryClient.open(
            args.port,
            ultrasonic_index=args.ultrasonic_index,
            timeout_seconds=args.timeout,
        ) as client:
            bring_up = client.initialize()
            print(json.dumps({"bring_up": asdict(bring_up)}, separators=(",", ":")))
            for sample_index in range(args.samples):
                snapshot = client.read_snapshot()
                payload = asdict(snapshot)
                payload["motors_stationary"] = snapshot.motors_stationary
                payload["ultrasonic_out_of_range"] = snapshot.ultrasonic_out_of_range
                print(json.dumps({"telemetry": payload}, separators=(",", ":")))
                if sample_index + 1 < args.samples:
                    time.sleep(args.interval)
    except (CyberPiClientError, CyberPiProtocolError, OSError) as error:
        print(f"telemetry probe failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
