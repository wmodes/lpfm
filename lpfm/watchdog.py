"""Watchdog — monitors stream health and triggers recovery or fallback on failure.

Polls the stream fetcher on a fixed interval. On failure it attempts to restart
the stream, backing off between attempts. If restarts are exhausted it declares
the stream dead, cuts the transmitter, and activates the fallback player. It
continues polling in the background and restores normal operation when the
stream recovers.

FallbackPlayer and Scheduler are optional at construction — if not provided,
fallback audio and broadcast-window checks are skipped. This allows the watchdog
to be used before those components are implemented.
"""

import logging
import threading
import time

from lpfm.config_loader import WatchdogConfig
from lpfm.stream_fetcher import StreamFetcher, StreamFetcherError
from lpfm.relay_controller import RelayController, RelayError


class Watchdog:
    """Monitors the stream fetcher and coordinates recovery on failure.

    Runs a background polling thread that checks whether the stream fetcher
    is alive. On failure it attempts restarts with a cooldown between each.
    After restart_attempts consecutive failures it declares the stream dead,
    cuts the transmitter, and starts the fallback player. Restores normal
    operation when the stream recovers.

    Args:
        watchdog_config: Polling and retry parameters from config.
        stream_fetcher: The StreamFetcher instance to monitor and restart.
        relay_controller: The RelayController used to cut/restore transmitter power.
        fallback_player: Optional FallbackPlayer to activate during stream failure.
        scheduler: Optional Scheduler used to check broadcast windows on recovery.
    """

    def __init__(
        self,
        watchdog_config: WatchdogConfig,
        stream_fetcher: StreamFetcher,
        relay_controller: RelayController,
        fallback_player=None,
        scheduler=None,
    ):
        self._config = watchdog_config
        self._stream_fetcher = stream_fetcher
        self._relay_controller = relay_controller
        self._fallback_player = fallback_player
        self._scheduler = scheduler
        self._logger = logging.getLogger(__name__)

        self._stop_event = threading.Event()
        self._poll_thread = None

        # Failure tracking
        self._consecutive_failures = 0
        self._in_fallback = False
        self._last_restart_attempt = 0.0   # monotonic timestamp

    def start(self) -> None:
        """Start the background polling thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            self._logger.warning("Watchdog start() called but already running")
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="watchdog",
            daemon=True,
        )
        self._logger.info("Watchdog started")
        self._poll_thread.start()

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it to exit."""
        if not self._poll_thread or not self._poll_thread.is_alive():
            return
        self._logger.info("Watchdog stopping")
        self._stop_event.set()
        self._poll_thread.join(timeout=self._config.poll_interval_seconds + 2)

    def is_stream_healthy(self) -> bool:
        """Return True if the stream fetcher is currently running."""
        return self._stream_fetcher.is_running()

    # ── Polling loop ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Main loop: wait poll_interval_seconds, then check stream health.

        Uses Event.wait() as the sleep so the thread wakes immediately when
        stop() is called rather than waiting out the full interval.
        """
        while not self._stop_event.wait(timeout=self._config.poll_interval_seconds):
            self._check_stream()

    def _check_stream(self) -> None:
        """Assess current stream state and act accordingly."""
        if self._stream_fetcher.is_running():
            self._handle_healthy_stream()
        else:
            self._handle_unhealthy_stream()

    # ── Healthy path ──────────────────────────────────────────────────────────

    def _handle_healthy_stream(self) -> None:
        """Handle a poll cycle where the stream is running."""
        if self._in_fallback:
            # Stream came back after a failure — begin recovery
            self._recover()
        else:
            # Normal healthy state, reset failure counter
            self._consecutive_failures = 0

    def _recover(self) -> None:
        """Restore normal operation after the stream recovers from failure."""
        self._logger.info("Stream recovered — restoring normal operation")
        self._in_fallback = False
        self._consecutive_failures = 0

        if self._fallback_player:
            self._fallback_player.stop()

        # Only restore the transmitter if we are inside a broadcast window
        in_window = self._scheduler.is_in_broadcast_window() if self._scheduler else True
        if in_window:
            try:
                self._relay_controller.turn_on()
            except RelayError as e:
                self._logger.error(f"Failed to restore transmitter after recovery: {e}")

    # ── Unhealthy path ────────────────────────────────────────────────────────

    def _handle_unhealthy_stream(self) -> None:
        """Handle a poll cycle where the stream is not running."""
        # Respect cooldown between restart attempts
        now = time.monotonic()
        seconds_since_last_attempt = now - self._last_restart_attempt
        if seconds_since_last_attempt < self._config.restart_cooldown_seconds:
            remaining = self._config.restart_cooldown_seconds - seconds_since_last_attempt
            self._logger.debug(f"Stream down — cooldown active, {remaining:.0f}s remaining")
            return

        if self._consecutive_failures >= self._config.restart_attempts:
            # Restart attempts exhausted — declare stream dead if not already in fallback
            if not self._in_fallback:
                self._declare_stream_dead()
            return

        # Attempt a restart
        self._consecutive_failures += 1
        self._last_restart_attempt = now
        self._logger.warning(
            f"Stream not running — restart attempt "
            f"{self._consecutive_failures}/{self._config.restart_attempts}"
        )
        try:
            self._stream_fetcher.start()
        except StreamFetcherError as e:
            self._logger.error(f"Restart attempt {self._consecutive_failures} failed: {e}")

    def _declare_stream_dead(self) -> None:
        """Cut the transmitter and activate fallback after all restarts failed."""
        self._logger.error(
            f"Stream failed after {self._config.restart_attempts} restart attempts — "
            "cutting transmitter, activating fallback"
        )
        self._in_fallback = True

        try:
            self._relay_controller.turn_off()
        except RelayError as e:
            self._logger.error(f"Failed to cut transmitter during stream failure: {e}")

        if self._fallback_player:
            self._fallback_player.start()
