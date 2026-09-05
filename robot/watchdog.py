"""Bridge SafetyController's heartbeat state to a real stop transmission.

`SafetyController` (safety.py) is a pure state machine: `watchdog_expired()` says
whether a heartbeat is overdue, but nothing calls it on a schedule or acts on the
answer. `HeartbeatWatchdog` is that missing loop — poll it, and the instant the
controller's watchdog trips, it latches the controller's estop *and* sends the
one real command this codebase can send, `CyberPiEmergencyStopClient.stop_all()`
(estop.py), so a lapsed heartbeat actually reaches the chassis rather than only
updating host state.

This proves the host-side half of "heartbeat-loss behavior": the host correctly
detects a stale heartbeat and transmits a real stop. It cannot prove the CyberPi
firmware would stop the chassis on its own if the host process vanished entirely
mid-drive — that half has no real content until something is actually driving,
so it is deferred to the movement bring-up step (see
docs/robot-control-contract.md).
"""

from __future__ import annotations

import time
from typing import Callable

from .estop import CyberPiEmergencyStopClient
from .safety import SafetyController


class HeartbeatWatchdog:
    """Poll a SafetyController; send one real stop per estop-latch event."""

    def __init__(
        self,
        controller: SafetyController,
        stop_client: CyberPiEmergencyStopClient,
        *,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.stop_client = stop_client
        self._on_stop = on_stop

    def poll_once(self, now: float | None = None) -> bool:
        """Check the watchdog once. Returns True iff this call sent a real stop.

        Idempotent across repeated calls during one expiry: `emergency_stop()`
        latches `controller.estopped`, and this only fires while that flag was
        not already set, so polling every few milliseconds does not spam the
        link with repeated stop commands while the link is already stopped.
        """

        if not self.controller.connected or self.controller.estopped:
            return False
        if not self.controller.watchdog_expired(now):
            return False
        self.controller.emergency_stop()
        self.stop_client.stop_all()
        if self._on_stop is not None:
            self._on_stop()
        return True

    def run_until(self, deadline_monotonic: float, *, poll_interval_seconds: float = 0.05) -> bool:
        """Poll in a loop until a wall-clock deadline; for bench/CLI use."""

        stopped = False
        while time.monotonic() < deadline_monotonic:
            if self.poll_once():
                stopped = True
            time.sleep(poll_interval_seconds)
        return stopped
