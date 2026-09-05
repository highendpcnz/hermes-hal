"""Host-side motion guards for the provisional robot control contract."""

from dataclasses import dataclass
import time

from .protocol import (
    CMD_DRIVE_DISTANCE,
    CMD_ESTOP,
    CMD_ROTATE_ANGLE,
    Telemetry,
    encode_drive_distance,
    encode_frame,
    encode_rotate_angle,
)


class SafetyError(ValueError):
    """Raised when a robot action is unsafe or outside the configured limits."""


@dataclass(frozen=True, slots=True)
class MotionLimits:
    """Conservative defaults for bench bring-up, not production tuning."""

    max_distance_cm: int = 50
    max_speed_pct: int = 30
    max_turn_degrees: int = 180
    min_obstacle_cm: float = 8.0
    watchdog_seconds: float = 0.25
    # How old the proximity reading may be at the moment motion is validated.
    # The watchdog above bounds the *link*; this bounds the *world model*, and
    # they are different failures: a caller can heartbeat perfectly while
    # arming against a minute-old obstacle distance. Deliberately generous —
    # `brain/gemma.py`'s `_run_motion` samples immediately before arming, so
    # this never fires on the real path; it exists to stop a future caller
    # from reusing a stale sample the way `CrawlLimits.max_frame_age_seconds`
    # stops one from reusing a stale camera frame.
    max_telemetry_age_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_distance_cm <= 0:
            raise SafetyError("max_distance_cm must be positive")
        if not 1 <= self.max_speed_pct <= 100:
            raise SafetyError("max_speed_pct must be between 1 and 100")
        if not 1 <= self.max_turn_degrees <= 180:
            raise SafetyError("max_turn_degrees must be between 1 and 180")
        if self.min_obstacle_cm < 0:
            raise SafetyError("min_obstacle_cm cannot be negative")
        if self.watchdog_seconds <= 0:
            raise SafetyError("watchdog_seconds must be positive")
        if self.max_telemetry_age_seconds <= 0:
            raise SafetyError("max_telemetry_age_seconds must be positive")


class SafetyController:
    """Gate motion until the link is live, armed, and recently heartbeating."""

    def __init__(self, limits: MotionLimits | None = None) -> None:
        self.limits = limits or MotionLimits()
        self.connected = False
        self.estopped = True
        self.last_telemetry: Telemetry | None = None
        self._last_telemetry_at: float | None = None
        self._last_heartbeat: float | None = None

    def connect(self, now: float | None = None) -> None:
        self.connected = True
        self._last_heartbeat = time.monotonic() if now is None else now

    def disconnect(self) -> None:
        self.connected = False
        self.estopped = True
        self._last_heartbeat = None
        # A sample taken over a link that has since dropped says nothing about
        # the world the robot would move through on the next connection.
        self.last_telemetry = None
        self._last_telemetry_at = None

    def arm(self, now: float | None = None) -> None:
        """Clear the host-side estop after an explicit operator check."""

        if not self.connected:
            raise SafetyError("cannot arm a disconnected robot")
        self.estopped = False
        self.heartbeat(now)

    def heartbeat(self, now: float | None = None) -> None:
        if not self.connected:
            raise SafetyError("cannot heartbeat a disconnected robot")
        self._last_heartbeat = time.monotonic() if now is None else now

    def update_telemetry(self, telemetry: Telemetry, now: float | None = None) -> None:
        """Record a sample and when it was taken.

        `now` follows the same injected-clock convention as `connect`/`arm`/
        `heartbeat`: pass it whenever those are passed one, so the age check in
        `_assert_telemetry_fresh` compares two readings from the same clock.
        """

        self.last_telemetry = telemetry
        self._last_telemetry_at = time.monotonic() if now is None else now

    def watchdog_expired(self, now: float | None = None) -> bool:
        if not self.connected or self._last_heartbeat is None:
            return True
        current = time.monotonic() if now is None else now
        return current - self._last_heartbeat > self.limits.watchdog_seconds

    def watchdog_stop(self, now: float | None = None) -> bytes | None:
        if not self.watchdog_expired(now):
            return None
        return self.emergency_stop()

    def request_drive(self, distance_cm: int, speed_pct: int, now: float | None = None) -> bytes:
        self.prepare_drive(distance_cm, speed_pct, now)
        return encode_frame(CMD_DRIVE_DISTANCE, encode_drive_distance(distance_cm * 10, speed_pct))

    def request_turn(self, angle_degrees: int, speed_pct: int, now: float | None = None) -> bytes:
        self.prepare_turn(angle_degrees, speed_pct, now)
        return encode_frame(CMD_ROTATE_ANGLE, encode_rotate_angle(angle_degrees, speed_pct))

    def prepare_drive(self, distance_cm: int, speed_pct: int, now: float | None = None) -> None:
        """Validate a drive request and refresh the heartbeat, without encoding
        a wire frame. Shared by the simulator's `request_drive` above and by
        `robot/motion.py`'s real CyberPi client, so both apply identical bounds
        before anything is ever transmitted."""

        self._assert_ready(now)
        if isinstance(distance_cm, bool) or not isinstance(distance_cm, int):
            raise SafetyError("distance_cm must be an integer")
        if distance_cm == 0:
            raise SafetyError("zero-distance drive is not a motion command")
        if abs(distance_cm) > self.limits.max_distance_cm:
            raise SafetyError(f"distance exceeds {self.limits.max_distance_cm} cm limit")
        self._validate_speed(speed_pct)
        telemetry = self._assert_telemetry_fresh(now)
        if distance_cm > 0 and telemetry.obstacle_dist_cm <= self.limits.min_obstacle_cm:
            raise SafetyError("forward motion blocked by the proximity interlock")
        self.heartbeat(now)

    def prepare_turn(self, angle_degrees: int, speed_pct: int, now: float | None = None) -> None:
        """Validate a turn request and refresh the heartbeat; see `prepare_drive`."""

        self._assert_ready(now)
        if isinstance(angle_degrees, bool) or not isinstance(angle_degrees, int):
            raise SafetyError("angle_degrees must be an integer")
        if angle_degrees == 0:
            raise SafetyError("zero-degree turn is not a motion command")
        if abs(angle_degrees) > self.limits.max_turn_degrees:
            raise SafetyError(f"turn exceeds {self.limits.max_turn_degrees} degree limit")
        self._validate_speed(speed_pct)
        # A turn in place has no obstacle ahead to check, but a robot whose
        # last sample is minutes old is one whose state is unknown either way.
        self._assert_telemetry_fresh(now)
        self.heartbeat(now)

    def emergency_stop(self) -> bytes:
        """Latch the host-side estop and return the wire command to transmit."""

        self.estopped = True
        return encode_frame(CMD_ESTOP)

    def _assert_telemetry_fresh(self, now: float | None) -> Telemetry:
        """Return the current sample, refusing one that is missing or stale.

        A negative age (an injected clock running behind the one that recorded
        the sample) counts as fresh: only *old* readings are a hazard.
        """

        telemetry = self.last_telemetry
        if telemetry is None or self._last_telemetry_at is None:
            raise SafetyError("motion requires a telemetry sample")
        current = time.monotonic() if now is None else now
        if current - self._last_telemetry_at > self.limits.max_telemetry_age_seconds:
            raise SafetyError(
                f"telemetry is older than the {self.limits.max_telemetry_age_seconds:g}s limit; "
                "take a fresh sample before moving"
            )
        return telemetry

    def _assert_ready(self, now: float | None) -> None:
        if not self.connected:
            raise SafetyError("robot is disconnected")
        if self.estopped:
            raise SafetyError("robot is emergency-stopped; arm it first")
        if self.watchdog_expired(now):
            self.emergency_stop()
            raise SafetyError("robot heartbeat expired")

    def _validate_speed(self, speed_pct: int) -> None:
        if isinstance(speed_pct, bool) or not isinstance(speed_pct, int):
            raise SafetyError("speed_pct must be an integer")
        if not 1 <= speed_pct <= self.limits.max_speed_pct:
            raise SafetyError(f"speed must be between 1 and {self.limits.max_speed_pct}%")
