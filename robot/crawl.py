"""Fail-closed limits for experimental camera-first crawl autonomy.

This module deliberately does not inspect pixels or control motors. It owns
the state machine between those systems: a fresh camera assessment must be
recorded before each short forward segment, and every constraint is checked
again by the normal motion safety controller immediately before transmission.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Literal


class CrawlSafetyError(ValueError):
    """A camera-first crawl request failed one of its mandatory safety gates."""


Assessment = Literal["clear", "blocked", "unknown"]


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    """Defaults for an experimental supervised mode.

    Loosened 2026-09-06, after the first hardware-verified episode (3 of 5
    possible 5cm segments, all clear, before the 60s clock ran out — see
    docs/technology-transfer.md). The loosening is deliberately one-axis:
    total distance and episode duration, which bound how *long* an
    unsupervised episode may run, not how *fast* or how *blind* any single
    motion is. max_speed_pct, min_obstacle_cm and min_vision_confidence are
    untouched — those are what keeps one segment survivable if the
    description-based assessment is wrong (see the module note on Ollama
    Cloud's tool-result 500 for why that assessment reads prose, not pixels).
    """

    max_segment_cm: int = 15
    max_speed_pct: int = 10
    max_total_distance_cm: int = 200
    max_duration_seconds: float = 300.0
    max_frame_age_seconds: float = 15.0
    min_vision_confidence: float = 0.9
    min_obstacle_cm: float = 25.0

    def __post_init__(self) -> None:
        if self.max_segment_cm <= 0:
            raise CrawlSafetyError("max_segment_cm must be positive")
        if not 1 <= self.max_speed_pct <= 30:
            raise CrawlSafetyError("max_speed_pct must be between 1 and 30")
        if self.max_total_distance_cm < self.max_segment_cm:
            raise CrawlSafetyError("max_total_distance_cm must cover one segment")
        if self.max_duration_seconds <= 0 or self.max_frame_age_seconds <= 0:
            raise CrawlSafetyError("crawl durations must be positive")
        if not 0.0 <= self.min_vision_confidence <= 1.0:
            raise CrawlSafetyError("min_vision_confidence must be between 0 and 1")
        if self.min_obstacle_cm < 8.0:
            raise CrawlSafetyError("min_obstacle_cm cannot weaken the base interlock")


@dataclass(slots=True)
class CrawlState:
    """Ephemeral state for exactly one browser or trusted-mission session."""

    active: bool = False
    remaining_cm: int = 0
    expires_at: float = 0.0
    capture_id: str | None = None
    captured_at: float = 0.0
    assessment: Assessment = "unknown"
    assessment_confidence: float = 0.0


class CrawlController:
    """Require capture -> assessment -> one forward segment, then repeat."""

    def __init__(self, limits: CrawlLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self.state = CrawlState()

    def arm(self) -> CrawlState:
        now = self._clock()
        self.state = CrawlState(
            active=True,
            remaining_cm=self.limits.max_total_distance_cm,
            expires_at=now + self.limits.max_duration_seconds,
        )
        return self.state

    def disarm(self) -> None:
        self.state.active = False
        self.state.assessment = "unknown"
        self.state.assessment_confidence = 0.0

    def is_active(self) -> bool:
        if self.state.active and self._clock() >= self.state.expires_at:
            self.disarm()
        return self.state.active

    def record_capture(self, capture_id: str) -> None:
        self._require_active()
        if not capture_id:
            raise CrawlSafetyError("crawl capture needs an id")
        self.state.capture_id = capture_id
        self.state.captured_at = self._clock()
        self.state.assessment = "unknown"
        self.state.assessment_confidence = 0.0

    def record_assessment(
        self, capture_id: str, assessment: Assessment, confidence: float
    ) -> None:
        self._require_fresh_capture(capture_id)
        if assessment not in ("clear", "blocked", "unknown"):
            raise CrawlSafetyError("crawl assessment must be clear, blocked, or unknown")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise CrawlSafetyError("crawl confidence must be numeric")
        if not 0.0 <= float(confidence) <= 1.0:
            raise CrawlSafetyError("crawl confidence must be between 0 and 1")
        self.state.assessment = assessment
        self.state.assessment_confidence = float(confidence)

    def prepare_drive(self, distance_cm: object, speed_pct: object) -> None:
        self._require_active()
        self._require_fresh_capture(self.state.capture_id)
        if self.state.assessment != "clear":
            raise CrawlSafetyError("crawl requires a current clear camera assessment")
        if self.state.assessment_confidence < self.limits.min_vision_confidence:
            raise CrawlSafetyError("crawl camera assessment confidence is too low")
        if isinstance(distance_cm, bool) or not isinstance(distance_cm, int):
            raise CrawlSafetyError("crawl distance_cm must be an integer")
        if not 1 <= distance_cm <= self.limits.max_segment_cm:
            raise CrawlSafetyError(
                f"crawl only permits forward segments from 1 to {self.limits.max_segment_cm} cm"
            )
        if distance_cm > self.state.remaining_cm:
            raise CrawlSafetyError("crawl distance budget is exhausted")
        if isinstance(speed_pct, bool) or not isinstance(speed_pct, int):
            raise CrawlSafetyError("crawl speed_pct must be an integer")
        if not 1 <= speed_pct <= self.limits.max_speed_pct:
            raise CrawlSafetyError(
                f"crawl speed must be between 1 and {self.limits.max_speed_pct}%"
            )

    def consume_drive(self, distance_cm: int) -> bool:
        """Spend the assessment and distance budget before hardware opens.

        Returning False signals that the fixed budget is complete and the
        controller has disarmed itself. A failed hardware command still uses
        the segment: retrying after an uncertain physical outcome is unsafe.
        """
        self.state.remaining_cm -= distance_cm
        self.state.assessment = "unknown"
        self.state.assessment_confidence = 0.0
        self.state.capture_id = None
        self.state.captured_at = 0.0
        if self.state.remaining_cm == 0:
            self.disarm()
        return self.state.active

    def _require_active(self) -> None:
        if not self.is_active():
            raise CrawlSafetyError("crawl autonomy is not armed or has expired")

    def _require_fresh_capture(self, capture_id: str | None) -> None:
        if not capture_id or capture_id != self.state.capture_id:
            raise CrawlSafetyError("crawl assessment does not match the latest camera capture")
        if self._clock() - self.state.captured_at > self.limits.max_frame_age_seconds:
            raise CrawlSafetyError("crawl camera capture is stale")
