"""Deterministic fake robot used before a CyberPi transport exists."""

from dataclasses import dataclass, field

from .protocol import Frame, Telemetry, decode_frame, encode_telemetry_frame
from .safety import MotionLimits, SafetyController


@dataclass(slots=True)
class SimulatedRobot:
    """A command logger with the same safety boundary as a real controller."""

    limits: MotionLimits = field(default_factory=MotionLimits)
    telemetry: Telemetry = field(
        default_factory=lambda: Telemetry(
            left_ticks=0,
            right_ticks=0,
            yaw_deg=0.0,
            pitch_deg=0.0,
            obstacle_dist_cm=100.0,
            battery_volts=3.9,
        )
    )
    safety: SafetyController = field(init=False)
    commands: list[Frame] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.safety = SafetyController(self.limits)

    def connect(self, now: float = 0.0) -> None:
        self.safety.connect(now)

    def arm(self, now: float = 0.0) -> None:
        self.safety.arm(now)
        self.safety.update_telemetry(self.telemetry, now)

    def heartbeat(self, now: float = 0.0) -> None:
        self.safety.heartbeat(now)

    def set_telemetry(self, telemetry: Telemetry, now: float = 0.0) -> None:
        self.telemetry = telemetry
        self.safety.update_telemetry(telemetry, now)

    def telemetry_frame(self) -> bytes:
        return encode_telemetry_frame(self.telemetry)

    def drive_distance(self, distance_cm: int, speed_pct: int, now: float = 0.0) -> bytes:
        raw = self.safety.request_drive(distance_cm, speed_pct, now)
        self.commands.append(decode_frame(raw))
        return raw

    def rotate(self, angle_degrees: int, speed_pct: int, now: float = 0.0) -> bytes:
        raw = self.safety.request_turn(angle_degrees, speed_pct, now)
        self.commands.append(decode_frame(raw))
        return raw

    def emergency_stop(self) -> bytes:
        raw = self.safety.emergency_stop()
        self.commands.append(decode_frame(raw))
        return raw
