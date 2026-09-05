"""Provisional, transport-independent control protocol for Project Odyssey.

The wire format here is a host/firmware contract under development. It is not
assumed to be the protocol shipped by Makeblock. Keep it isolated until the
actual CyberPi firmware and USB behavior have been verified on hardware.
"""

from dataclasses import dataclass
from typing import Final
import struct

MAGIC: Final = b"\xAA\x55"
MAX_PAYLOAD_BYTES: Final = 32
TELEMETRY_FRAME: Final = 0x10
CMD_VELOCITY: Final = 0x01
CMD_DRIVE_DISTANCE: Final = 0x02
CMD_ROTATE_ANGLE: Final = 0x03
CMD_SET_LEDS: Final = 0x04
CMD_ESTOP: Final = 0x05

_TELEMETRY = struct.Struct("<iihhHH")
_FRAME_OVERHEAD = len(MAGIC) + 1 + 1 + 2


class ProtocolError(ValueError):
    """Base error for malformed or invalid robot frames."""


class FrameChecksumError(ProtocolError):
    """Raised when a frame's CRC does not match its contents."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded message without its framing bytes or CRC."""

    message_id: int
    payload: bytes

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not 0 <= self.message_id <= 0xFF:
            raise ProtocolError("message_id must be an integer from 0 to 255")
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise ProtocolError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The normalized telemetry shape shared by transports and the UI."""

    left_ticks: int
    right_ticks: int
    yaw_deg: float
    pitch_deg: float
    obstacle_dist_cm: float
    battery_volts: float


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """Return CRC-16/CCITT-FALSE for *data*."""

    crc = initial & 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def encode_frame(message_id: int, payload: bytes = b"") -> bytes:
    """Encode one frame using little-endian CRC bytes.

    The checksum covers the message ID, payload length, and payload, but not
    the magic prefix. This makes resynchronization possible after line noise.
    """

    frame = Frame(message_id, bytes(payload))
    body = bytes((frame.message_id, len(frame.payload))) + frame.payload
    return MAGIC + body + struct.pack("<H", crc16_ccitt(body))


def decode_frame(raw: bytes) -> Frame:
    """Decode exactly one complete frame."""

    if len(raw) < _FRAME_OVERHEAD:
        raise ProtocolError("frame is shorter than the protocol header")
    if raw[:2] != MAGIC:
        raise ProtocolError("frame has an invalid magic prefix")

    payload_length = raw[3]
    expected_length = _FRAME_OVERHEAD + payload_length
    if len(raw) != expected_length:
        raise ProtocolError(
            f"frame length is {len(raw)} bytes; expected {expected_length}"
        )

    body = raw[2:-2]
    expected_crc = crc16_ccitt(body)
    actual_crc = int.from_bytes(raw[-2:], "little")
    if actual_crc != expected_crc:
        raise FrameChecksumError(
            f"CRC mismatch: received 0x{actual_crc:04x}, expected 0x{expected_crc:04x}"
        )
    return Frame(raw[2], bytes(raw[4:-2]))


class FrameDecoder:
    """Incrementally decode frames from arbitrary serial chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.crc_errors = 0
        self.malformed_frames = 0
        self.dropped_bytes = 0
        self.frames_decoded = 0

    def feed(self, data: bytes) -> list[Frame]:
        """Consume a chunk and return every complete valid frame it contains."""

        self._buffer.extend(data)
        decoded: list[Frame] = []

        while True:
            if len(self._buffer) < 4:
                break

            start = self._buffer.find(MAGIC)
            if start < 0:
                # Preserve a possible first magic byte split across chunks.
                if self._buffer[-1:] == MAGIC[:1]:
                    self.dropped_bytes += len(self._buffer) - 1
                    self._buffer[:] = self._buffer[-1:]
                else:
                    self.dropped_bytes += len(self._buffer)
                    self._buffer.clear()
                break
            if start:
                self.dropped_bytes += start
                del self._buffer[:start]

            if len(self._buffer) < 4:
                break
            total_length = _FRAME_OVERHEAD + self._buffer[3]
            if len(self._buffer) < total_length:
                break

            raw = bytes(self._buffer[:total_length])
            try:
                frame = decode_frame(raw)
            except FrameChecksumError:
                self.crc_errors += 1
                # Drop one byte only; a valid magic prefix may begin inside the
                # rejected frame and should remain available for resync.
                del self._buffer[0]
                continue
            except ProtocolError:
                self.malformed_frames += 1
                del self._buffer[0]
                continue

            del self._buffer[:total_length]
            self.frames_decoded += 1
            decoded.append(frame)

        return decoded


def _bounded_int(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be between {minimum} and {maximum}")
    return value


def _speed_payload(speed_pct: int) -> int:
    return _bounded_int("speed_pct", speed_pct, 0, 100)


def encode_drive_distance(distance_mm: int, speed_pct: int) -> bytes:
    """Encode a signed millimetre distance and percentage speed."""

    _bounded_int("distance_mm", distance_mm, -(2**31), 2**31 - 1)
    return struct.pack("<iB", distance_mm, _speed_payload(speed_pct))


def encode_rotate_angle(angle_degrees: int, speed_pct: int) -> bytes:
    """Encode a signed degree angle and percentage speed."""

    _bounded_int("angle_degrees", angle_degrees, -180, 180)
    return struct.pack("<hB", angle_degrees, _speed_payload(speed_pct))


def encode_telemetry_frame(telemetry: Telemetry) -> bytes:
    """Encode normalized telemetry for the simulator and contract tests."""

    payload = _TELEMETRY.pack(
        _bounded_int("left_ticks", telemetry.left_ticks, -(2**31), 2**31 - 1),
        _bounded_int("right_ticks", telemetry.right_ticks, -(2**31), 2**31 - 1),
        _bounded_int("yaw_deg", round(telemetry.yaw_deg * 10), -32768, 32767),
        _bounded_int("pitch_deg", round(telemetry.pitch_deg * 10), -32768, 32767),
        _bounded_int("obstacle_dist_cm", round(telemetry.obstacle_dist_cm * 10), 0, 65535),
        _bounded_int("battery_volts", round(telemetry.battery_volts * 1000), 0, 65535),
    )
    return encode_frame(TELEMETRY_FRAME, payload)


def decode_telemetry(frame: Frame) -> Telemetry:
    """Decode a telemetry frame into SI-friendly units."""

    if frame.message_id != TELEMETRY_FRAME:
        raise ProtocolError(f"expected telemetry frame, got 0x{frame.message_id:02x}")
    if len(frame.payload) != _TELEMETRY.size:
        raise ProtocolError(f"telemetry payload must be {_TELEMETRY.size} bytes")

    left_ticks, right_ticks, yaw, pitch, distance_mm, battery_mv = _TELEMETRY.unpack(
        frame.payload
    )
    return Telemetry(
        left_ticks=left_ticks,
        right_ticks=right_ticks,
        yaw_deg=yaw / 10,
        pitch_deg=pitch / 10,
        obstacle_dist_cm=distance_mm / 10,
        battery_volts=battery_mv / 1000,
    )
