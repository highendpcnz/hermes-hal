"""Decoder and encoder for the observed CyberPi/mBlock serial profile.

The frame shape is inferred from mBlock's bundled CyberPi mode markers and
the boot bytes captured from this device.
"""

import ast
from dataclasses import dataclass
from enum import StrEnum
import struct
from typing import Final


USB_VENDOR_ID: Final = 6790
USB_PRODUCT_ID: Final = 29987
BAUD_RATE: Final = 115200
PROTOCOL_NAME: Final = "f3f4"

FRAME_START: Final = 0xF3
FRAME_END: Final = 0xF4
ONLINE_PROTOCOL_ID: Final = 0x28
ONLINE_NO_WAIT: Final = 0x00
ONLINE_WAIT: Final = 0x01
SUBSCRIBE_PROTOCOL_ID: Final = 0x29
SUBSCRIBE_REPORT_COMMAND: Final = 0x00
MODE_QUERY_ID: Final = 0x0D
MODE_QUERY_COMMAND: Final = 0x80
FIRMWARE_QUERY_ID: Final = 0x06
MAX_PAYLOAD_BYTES: Final = 0xFFFF
MAX_ONLINE_SCRIPT_BYTES: Final = 249
_FRAME_OVERHEAD = 6
_ONLINE_REQUEST_HEADER = 6
_SUBSCRIPTION_REPORT_HEADER = 4
_START_BYTE = bytes((FRAME_START,))

ONLINE_MODE_MARKER: Final = bytes.fromhex("f3 f6 03 00 0d 00 01 0e f4")
"""The real online-mode command: `0x0D 0x00 0x01` (mode-query uses command
byte 0x80; this is a *different* command, 0x00, carrying a target-mode byte).
Originally added only as a decode target inferred from mBlock's bundled
strings; a full cold-boot USB capture of a real mBlock "Enter Live" session
showed mBlock sending this exact frame host-to-device, and a direct live
round-trip test (send this, mode flips to online; send UPLOAD_MODE_MARKER,
mode flips back to upload; repeatable) confirmed it standalone -- see
docs/termux-usb-bringup.md. The `online_restart`/`config.write_config`
scripts mBlock also sends around the same time (formerly ONLINE_ENTRY_SCRIPTS
here) are not needed for this: a raw, unwrapped `online_restart` call was
hardware-confirmed to execute cleanly and do nothing to the reported mode."""
UPLOAD_MODE_MARKER: Final = bytes.fromhex("f3 f6 03 00 0d 00 00 0d f4")
"""The upload-mode counterpart to ONLINE_MODE_MARKER -- see its docstring."""


class CyberPiProtocolError(ValueError):
    """Raised when an observed CyberPi frame is malformed."""


class CyberPiChecksumError(CyberPiProtocolError):
    """Raised when an observed CyberPi header or payload checksum is invalid."""


class CyberPiMode(StrEnum):
    """The two mBlock mode markers identified in the installed app."""

    ONLINE = "online"
    UPLOAD = "upload"


@dataclass(frozen=True, slots=True)
class CyberPiOnlineRequest:
    """The verified request payload layout used by mBlock's online manager."""

    sequence: int
    wait_for_response: bool
    script: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= 0xFFFF:
            raise CyberPiProtocolError("sequence must be an integer from 0 to 65535")
        if not isinstance(self.wait_for_response, bool):
            raise CyberPiProtocolError("wait_for_response must be a boolean")
        if not isinstance(self.script, str):
            raise CyberPiProtocolError("script must be a string")


@dataclass(frozen=True, slots=True)
class CyberPiOnlineResponse:
    """A decoded return value from a wait-for-response online request."""

    sequence: int
    result: object = None
    error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= 0xFFFF:
            raise CyberPiProtocolError("sequence must be an integer from 0 to 65535")
        if self.error is not None and not isinstance(self.error, str):
            raise CyberPiProtocolError("error must be a string or None")


@dataclass(frozen=True, slots=True)
class CyberPiFrame:
    """A decoded f3f4 frame with its validated checksum fields."""

    payload: bytes
    header_checksum: int
    payload_checksum: int

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes):
            raise CyberPiProtocolError("payload must be bytes")
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise CyberPiProtocolError("payload exceeds the two-byte length field")
        if isinstance(self.header_checksum, bool) or not 0 <= self.header_checksum <= 0xFF:
            raise CyberPiProtocolError("header_checksum must be an integer from 0 to 255")
        if isinstance(self.payload_checksum, bool) or not 0 <= self.payload_checksum <= 0xFF:
            raise CyberPiProtocolError("payload_checksum must be an integer from 0 to 255")
        if self.header_checksum != _header_checksum(len(self.payload)):
            raise CyberPiChecksumError("header_checksum does not match the payload length")
        if self.payload_checksum != _payload_checksum(self.payload):
            raise CyberPiChecksumError("payload_checksum does not match the payload")


def _header_checksum(payload_length: int) -> int:
    """Return the additive checksum over the start byte and length bytes."""

    return (FRAME_START + (payload_length & 0xFF) + (payload_length >> 8)) & 0xFF


@dataclass(frozen=True, slots=True)
class CyberPiTransportProfile:
    """Observed USB identity and serial settings for this CyberPi path."""

    vendor_id: int = USB_VENDOR_ID
    product_id: int = USB_PRODUCT_ID
    baud_rate: int = BAUD_RATE
    protocol_name: str = PROTOCOL_NAME
    online_marker: bytes = ONLINE_MODE_MARKER
    upload_marker: bytes = UPLOAD_MODE_MARKER

    def matches_usb(self, vendor_id: int, product_id: int) -> bool:
        return vendor_id == self.vendor_id and product_id == self.product_id


DEFAULT_PROFILE: Final = CyberPiTransportProfile()


def _payload_checksum(payload: bytes) -> int:
    """Return the additive checksum observed in mBlock's mode frames."""

    return sum(payload) & 0xFF


def encode_f3f4_frame(payload: bytes) -> bytes:
    """Wrap a payload in the f3f4 framing used by mBlock's CyberPi profile."""

    payload = bytes(payload)
    payload_length = len(payload)
    if payload_length > MAX_PAYLOAD_BYTES:
        raise CyberPiProtocolError("payload exceeds the two-byte length field")
    length_bytes = bytes((payload_length & 0xFF, payload_length >> 8))
    return bytes((FRAME_START, _header_checksum(payload_length))) + length_bytes + payload + bytes(
        (_payload_checksum(payload), FRAME_END)
    )


def encode_current_mode_query_frame() -> bytes:
    """Build the observed, non-mutating current-mode query frame."""

    return encode_f3f4_frame(bytes((MODE_QUERY_ID, MODE_QUERY_COMMAND)))


def encode_firmware_version_query_frame() -> bytes:
    """Build the observed, non-mutating firmware-version query frame."""

    return encode_f3f4_frame(bytes((FIRMWARE_QUERY_ID,)))


def decode_current_mode_response(payload: bytes) -> CyberPiMode:
    """Decode the observed three-byte response to the current-mode query."""

    if len(payload) != 3 or payload[:2] != bytes((MODE_QUERY_ID, MODE_QUERY_COMMAND)):
        raise CyberPiProtocolError("payload is not a current-mode response")
    if payload[2] == ONLINE_WAIT:
        return CyberPiMode.ONLINE
    if payload[2] == ONLINE_NO_WAIT:
        return CyberPiMode.UPLOAD
    raise CyberPiProtocolError(f"unknown CyberPi mode value: 0x{payload[2]:02x}")


def decode_firmware_version_response(payload: bytes) -> str:
    """Decode the fixed-width firmware-version response observed from CyberPi."""

    if len(payload) != 10 or payload[0] != FIRMWARE_QUERY_ID:
        raise CyberPiProtocolError("payload is not a firmware-version response")
    try:
        return bytes(payload[1:]).decode("ascii")
    except UnicodeDecodeError as error:
        raise CyberPiProtocolError("firmware version is not ASCII") from error


def encode_online_request_payload(
    script: str, *, sequence: int = 0, wait_for_response: bool = False
) -> bytes:
    """Build the payload mBlock uses for an online Python-script request."""

    if not isinstance(script, str):
        raise CyberPiProtocolError("script must be a string")
    script_bytes = script.encode("utf-8")
    if len(script_bytes) > MAX_ONLINE_SCRIPT_BYTES:
        raise CyberPiProtocolError(
            f"online script exceeds the verified {MAX_ONLINE_SCRIPT_BYTES}-byte limit"
        )
    request = CyberPiOnlineRequest(sequence, wait_for_response, script)
    wait_byte = ONLINE_WAIT if request.wait_for_response else ONLINE_NO_WAIT
    script_length = len(script_bytes)
    return bytes(
        (
            ONLINE_PROTOCOL_ID,
            wait_byte,
            request.sequence & 0xFF,
            request.sequence >> 8,
            script_length & 0xFF,
            script_length >> 8,
        )
    ) + script_bytes


def encode_online_request_frame(
    script: str, *, sequence: int = 0, wait_for_response: bool = False
) -> bytes:
    """Build a complete, transport-free f3f4 online request frame."""

    return encode_f3f4_frame(
        encode_online_request_payload(
            script, sequence=sequence, wait_for_response=wait_for_response
        )
    )


def encode_online_mode_frame() -> bytes:
    """Return ONLINE_MODE_MARKER -- the real, hardware-confirmed command that
    switches CyberPi into online mode. See its docstring for the evidence."""

    return ONLINE_MODE_MARKER


def encode_upload_mode_frame() -> bytes:
    """Return UPLOAD_MODE_MARKER -- the real, hardware-confirmed command that
    switches CyberPi into upload mode. See ONLINE_MODE_MARKER's docstring."""

    return UPLOAD_MODE_MARKER


def decode_online_request_payload(payload: bytes) -> CyberPiOnlineRequest:
    """Decode one mBlock online request payload without executing its script."""

    if len(payload) < _ONLINE_REQUEST_HEADER:
        raise CyberPiProtocolError("online request is shorter than its header")
    if payload[0] != ONLINE_PROTOCOL_ID:
        raise CyberPiProtocolError("payload is not an online request")
    if payload[1] not in (ONLINE_NO_WAIT, ONLINE_WAIT):
        raise CyberPiProtocolError("online request has an invalid wait flag")

    sequence = struct.unpack_from("<H", payload, 2)[0]
    script_length = struct.unpack_from("<H", payload, 4)[0]
    expected_length = _ONLINE_REQUEST_HEADER + script_length
    if len(payload) != expected_length:
        raise CyberPiProtocolError(
            f"online request length is {len(payload)} bytes; expected {expected_length}"
        )
    try:
        script = bytes(payload[_ONLINE_REQUEST_HEADER:]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CyberPiProtocolError("online script is not valid UTF-8") from error
    return CyberPiOnlineRequest(
        sequence=sequence,
        wait_for_response=payload[1] == ONLINE_WAIT,
        script=script,
    )


def decode_online_response_payload(payload: bytes) -> CyberPiOnlineResponse:
    """Decode the common response returned by a wait-for-response request."""

    if len(payload) < _ONLINE_REQUEST_HEADER:
        raise CyberPiProtocolError("online response is shorter than its header")
    if payload[:2] != bytes((ONLINE_PROTOCOL_ID, ONLINE_WAIT)):
        raise CyberPiProtocolError("payload is not an online response")

    sequence = struct.unpack_from("<H", payload, 2)[0]
    response_length = struct.unpack_from("<H", payload, 4)[0]
    expected_length = _ONLINE_REQUEST_HEADER + response_length
    if len(payload) != expected_length:
        raise CyberPiProtocolError(
            f"online response length is {len(payload)} bytes; expected {expected_length}"
        )
    try:
        response_text = bytes(payload[_ONLINE_REQUEST_HEADER:]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CyberPiProtocolError("online response is not valid UTF-8") from error

    try:
        response = ast.literal_eval(response_text)
    except (SyntaxError, ValueError) as error:
        raise CyberPiProtocolError("online response is not a valid literal") from error
    if not isinstance(response, dict):
        raise CyberPiProtocolError("online response body must be a dictionary")
    if "ret" in response:
        return CyberPiOnlineResponse(sequence=sequence, result=response["ret"])
    if isinstance(response.get("err"), str):
        return CyberPiOnlineResponse(sequence=sequence, error=response["err"])
    raise CyberPiProtocolError("online response does not contain ret or err")


def decode_subscription_report(payload: bytes) -> dict[str, object]:
    """Decode an mBlock ``subscribe.add_item`` report payload safely."""

    if len(payload) < _SUBSCRIPTION_REPORT_HEADER:
        raise CyberPiProtocolError("subscription report is shorter than its header")
    if payload[0] != SUBSCRIBE_PROTOCOL_ID or payload[1] != SUBSCRIBE_REPORT_COMMAND:
        raise CyberPiProtocolError("payload is not a subscription report")

    report_length = struct.unpack_from("<H", payload, 2)[0]
    expected_length = _SUBSCRIPTION_REPORT_HEADER + report_length
    if len(payload) != expected_length:
        raise CyberPiProtocolError(
            f"subscription report length is {len(payload)} bytes; expected {expected_length}"
        )
    try:
        report_text = bytes(payload[_SUBSCRIPTION_REPORT_HEADER:]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise CyberPiProtocolError("subscription report is not valid UTF-8") from error

    try:
        report = ast.literal_eval(report_text)
    except (SyntaxError, ValueError) as error:
        raise CyberPiProtocolError("subscription report is not a valid literal") from error
    if not isinstance(report, dict) or any(not isinstance(key, str) for key in report):
        raise CyberPiProtocolError("subscription report must be a dictionary with string keys")
    return dict(report)


def decode_f3f4_frame(raw: bytes) -> CyberPiFrame:
    """Decode one observed f3f4 frame without assigning command semantics."""

    if len(raw) < _FRAME_OVERHEAD:
        raise CyberPiProtocolError("frame is shorter than the f3f4 header")
    if raw[0] != FRAME_START or raw[-1] != FRAME_END:
        raise CyberPiProtocolError("frame has invalid f3f4 delimiters")

    payload_length = struct.unpack_from("<H", raw, 2)[0]
    expected_length = _FRAME_OVERHEAD + payload_length
    if len(raw) != expected_length:
        raise CyberPiProtocolError(
            f"frame length is {len(raw)} bytes; expected {expected_length}"
        )

    actual_header_checksum = raw[1]
    expected_header_checksum = _header_checksum(payload_length)
    if actual_header_checksum != expected_header_checksum:
        raise CyberPiChecksumError(
            f"header checksum mismatch: received 0x{actual_header_checksum:02x}, "
            f"expected 0x{expected_header_checksum:02x}"
        )

    payload = bytes(raw[4 : 4 + payload_length])
    actual_checksum = raw[-2]
    expected_checksum = _payload_checksum(payload)
    if actual_checksum != expected_checksum:
        raise CyberPiChecksumError(
            f"checksum mismatch: received 0x{actual_checksum:02x}, "
            f"expected 0x{expected_checksum:02x}"
        )
    return CyberPiFrame(
        payload=payload,
        header_checksum=actual_header_checksum,
        payload_checksum=actual_checksum,
    )


class F3F4FrameDecoder:
    """Incrementally decode f3f4 frames from arbitrary serial chunks."""

    def __init__(self, max_payload_bytes: int = 4096) -> None:
        if not 1 <= max_payload_bytes <= MAX_PAYLOAD_BYTES:
            raise CyberPiProtocolError("max_payload_bytes is outside the wire range")
        self.max_payload_bytes = max_payload_bytes
        self._buffer = bytearray()
        self.checksum_errors = 0
        self.malformed_frames = 0
        self.dropped_bytes = 0
        self.frames_decoded = 0

    def feed(self, data: bytes) -> list[CyberPiFrame]:
        """Consume a chunk and return every complete valid frame it contains."""

        self._buffer.extend(data)
        decoded: list[CyberPiFrame] = []

        while True:
            if len(self._buffer) < 4:
                break

            start = self._buffer.find(_START_BYTE)
            if start < 0:
                if self._buffer[-1:] == _START_BYTE:
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
            payload_length = struct.unpack_from("<H", self._buffer, 2)[0]
            if payload_length > self.max_payload_bytes:
                self.malformed_frames += 1
                del self._buffer[0]
                continue

            if self._buffer[1] != _header_checksum(payload_length):
                self.checksum_errors += 1
                del self._buffer[0]
                continue

            total_length = _FRAME_OVERHEAD + payload_length
            if len(self._buffer) < total_length:
                break

            raw = bytes(self._buffer[:total_length])
            try:
                frame = decode_f3f4_frame(raw)
            except CyberPiChecksumError:
                self.checksum_errors += 1
                del self._buffer[0]
                continue
            except CyberPiProtocolError:
                self.malformed_frames += 1
                del self._buffer[0]
                continue

            del self._buffer[:total_length]
            self.frames_decoded += 1
            decoded.append(frame)

        return decoded


def decode_mode_marker(raw: bytes) -> CyberPiMode | None:
    """Return the mBlock mode represented by a valid marker, if recognized."""

    frame = decode_f3f4_frame(raw)
    if frame.payload == ONLINE_MODE_MARKER[4:-2]:
        return CyberPiMode.ONLINE
    if frame.payload == UPLOAD_MODE_MARKER[4:-2]:
        return CyberPiMode.UPLOAD
    return None


def extract_boot_lines(raw: bytes) -> tuple[str, ...]:
    """Extract recognizable MicroPython boot lines without decoding binary data."""

    prefixes = (b"PYB:", b"MicroPython")
    lines = []
    for line in raw.splitlines():
        if line.startswith(prefixes):
            lines.append(line.decode("ascii", "replace"))
    return tuple(lines)
