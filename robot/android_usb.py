"""CH340 USB-serial userspace driver for Android/Termux.

Android exposes no kernel `/dev/ttyUSB*` node for USB-serial adapters the way
desktop Linux does — Termux's `termux-usb` instead grants a raw, already-open
file descriptor for the USB device via Android's UsbManager permission
dialog (see docs/termux-usb-bringup.md for the bring-up story). A plain
`libusb_init()` fails on Android with `LIBUSB_ERROR_IO` because it tries to
enumerate the whole USB bus, which the sandbox blocks; `libusb_wrap_sys_device()`
(added to libusb specifically for this Android use case) wraps that
already-open fd directly, skipping enumeration entirely via
`LIBUSB_OPTION_NO_DEVICE_DISCOVERY`.

The CH340 register-write sequence in `_configure_ch340` is transcribed from
the Linux kernel's `drivers/usb/serial/ch341.c` (`ch341_configure` +
`ch341_open`), not reverse-engineered from scratch — same source as the
`f3f4` framing this driver ultimately carries (see `cyberpi.py`).

`Ch340UsbTransport` implements the same `SerialTransport` protocol
`telemetry.py`/`estop.py`/`motion.py` already depend on, so none of that
already hardware-verified code needs to change to run over this transport
instead of `pyserial`.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, Union, byref, c_char_p, c_int, c_int64, c_uint8, c_uint16, c_uint32, c_void_p

LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2
_LIBUSB_ERROR_TIMEOUT = -7

CH340_BAUDBASE_FACTOR = 1532620800
CH340_BAUDBASE_DIVMAX = 3
CH340_BIT_DTR = 1 << 5
CH340_BIT_RTS = 1 << 6

# USB_TYPE_VENDOR | USB_RECIP_DEVICE, OR'd with direction.
_TYPE_VENDOR_OUT = 0x40
_TYPE_VENDOR_IN = 0xC0

_LIBUSB_CANDIDATES = (
    "/data/data/com.termux/files/usr/lib/libusb-1.0.so",
    "libusb-1.0.so",
    "libusb-1.0.so.0",
)


class _InitOptionValue(Union):
    _fields_ = [("ival", c_int), ("log_cbval", c_void_p)]


class _InitOption(Structure):
    _fields_ = [("option", c_int), ("value", _InitOptionValue)]


class Ch340UsbError(RuntimeError):
    """Raised on any libusb-level failure talking to the CH340 bridge."""


def _load_libusb() -> ctypes.CDLL:
    last_error: OSError | None = None
    for name in _LIBUSB_CANDIDATES:
        try:
            return ctypes.CDLL(name)
        except OSError as error:
            last_error = error
    raise Ch340UsbError(f"could not load libusb: {last_error}")


class Ch340UsbTransport:
    """A `SerialTransport` implementation over a CH340 bridge reached via a
    raw fd already granted by `termux-usb`."""

    _IN_EP = 0x82
    _OUT_EP = 0x02

    def __init__(self, fd: int, *, baud_rate: int = 115200, read_timeout_ms: int = 50) -> None:
        self._lib = _load_libusb()
        self._declare_signatures()
        self._read_timeout_ms = read_timeout_ms
        self._closed = False

        self._ctx = c_void_p()
        option = _InitOption(option=LIBUSB_OPTION_NO_DEVICE_DISCOVERY, value=_InitOptionValue(ival=0))
        self._check(self._lib.libusb_init_context(byref(self._ctx), byref(option), 1), "init_context")

        self._handle = c_void_p()
        self._check(
            self._lib.libusb_wrap_sys_device(self._ctx, c_int64(fd), byref(self._handle)), "wrap_sys_device"
        )
        self._check(self._lib.libusb_claim_interface(self._handle, 0), "claim_interface")
        self._configure_ch340(baud_rate)

    def _declare_signatures(self) -> None:
        lib = self._lib
        lib.libusb_init_context.argtypes = [POINTER(c_void_p), POINTER(_InitOption), c_int]
        lib.libusb_init_context.restype = c_int
        lib.libusb_wrap_sys_device.argtypes = [c_void_p, c_int64, POINTER(c_void_p)]
        lib.libusb_wrap_sys_device.restype = c_int
        lib.libusb_claim_interface.argtypes = [c_void_p, c_int]
        lib.libusb_claim_interface.restype = c_int
        lib.libusb_control_transfer.argtypes = [
            c_void_p, c_uint8, c_uint8, c_uint16, c_uint16, c_char_p, c_uint16, c_uint32
        ]
        lib.libusb_control_transfer.restype = c_int
        lib.libusb_bulk_transfer.argtypes = [c_void_p, c_uint8, c_char_p, c_int, POINTER(c_int), c_uint32]
        lib.libusb_bulk_transfer.restype = c_int
        lib.libusb_release_interface.argtypes = [c_void_p, c_int]
        lib.libusb_release_interface.restype = c_int
        lib.libusb_close.argtypes = [c_void_p]
        lib.libusb_exit.argtypes = [c_void_p]
        lib.libusb_strerror.argtypes = [c_int]
        lib.libusb_strerror.restype = c_char_p

    def _check(self, ret: int, what: str) -> int:
        if ret < 0:
            message = self._lib.libusb_strerror(ret).decode("ascii", "replace")
            raise Ch340UsbError(f"{what} failed: {message} ({ret})")
        return ret

    def _control_out(self, request: int, value: int, index: int) -> None:
        self._check(
            self._lib.libusb_control_transfer(self._handle, _TYPE_VENDOR_OUT, request, value, index, None, 0, 1000),
            f"control_out(0x{request:02x}, 0x{value:04x}, 0x{index:04x})",
        )

    def _control_in(self, request: int, value: int, index: int, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        n = self._check(
            self._lib.libusb_control_transfer(
                self._handle, _TYPE_VENDOR_IN, request, value, index, buf, length, 1000
            ),
            f"control_in(0x{request:02x}, 0x{value:04x}, 0x{index:04x})",
        )
        return buf.raw[:n]

    @staticmethod
    def _baud_registers(baud_rate: int) -> tuple[int, int]:
        """Transcribed from ch341_set_baudrate() in the Linux kernel driver."""
        if baud_rate <= 0:
            raise Ch340UsbError("baud_rate must be positive")
        factor = CH340_BAUDBASE_FACTOR // baud_rate
        divisor = CH340_BAUDBASE_DIVMAX
        while factor > 0xFFF0 and divisor:
            factor >>= 3
            divisor -= 1
        if factor > 0xFFF0:
            raise Ch340UsbError(f"baud rate {baud_rate} out of range for CH340")
        factor = 0x10000 - factor
        a = (factor & 0xFF00) | divisor
        b = factor & 0xFF
        return a, b

    def _set_baudrate(self, baud_rate: int) -> None:
        a, b = self._baud_registers(baud_rate)
        self._control_out(0x9A, 0x1312, a)
        self._control_out(0x9A, 0x0F2C, b)

    def _set_handshake(self, control: int) -> None:
        self._control_out(0xA4, (~control) & 0xFFFF, 0)

    def _configure_ch340(self, baud_rate: int) -> None:
        """Reproduces ch341_configure() + ch341_open()'s register sequence."""
        self._control_in(0x5F, 0, 0, 8)
        self._control_out(0xA1, 0, 0)
        self._set_baudrate(baud_rate)
        self._control_in(0x95, 0x2518, 0, 8)
        self._control_out(0x9A, 0x2518, 0x0050)
        self._control_in(0x95, 0x0706, 0, 8)
        self._control_out(0xA1, 0x501F, 0xD90A)
        self._set_baudrate(baud_rate)
        self._set_handshake(CH340_BIT_DTR | CH340_BIT_RTS)
        self._control_in(0x95, 0x0706, 0, 8)

    # --- SerialTransport protocol (see robot/telemetry.py) -------------------

    @property
    def in_waiting(self) -> int:
        # libusb has no kernel read-buffer byte count to query. read() below
        # already blocks for at most read_timeout_ms and returns b"" on
        # timeout, so callers polling read(max(1, in_waiting)) behave
        # correctly regardless of what this reports — it only needs to stay
        # positive so those callers keep trying.
        return 64

    def read(self, size: int = 1) -> bytes:
        buf = ctypes.create_string_buffer(size)
        transferred = c_int(0)
        ret = self._lib.libusb_bulk_transfer(
            self._handle, self._IN_EP, buf, size, byref(transferred), self._read_timeout_ms
        )
        if ret != 0 and ret != _LIBUSB_ERROR_TIMEOUT:
            self._check(ret, "bulk_transfer(read)")
        return buf.raw[: transferred.value]

    def write(self, data: bytes) -> int:
        buf = ctypes.create_string_buffer(data, len(data))
        transferred = c_int(0)
        self._check(
            self._lib.libusb_bulk_transfer(self._handle, self._OUT_EP, buf, len(data), byref(transferred), 1000),
            "bulk_transfer(write)",
        )
        return transferred.value

    def flush(self) -> None:
        pass  # bulk_transfer() above is already synchronous.

    def reset_input_buffer(self) -> None:
        while self.read(64):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lib.libusb_release_interface(self._handle, 0)
        self._lib.libusb_close(self._handle)
        self._lib.libusb_exit(self._ctx)
