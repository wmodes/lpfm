"""Scheduler — makes daily broadcast decisions and controls on-air timing.

At a configured decision_time each day the scheduler calculates accumulated
risk, rolls a weighted probability, and — if broadcasting — picks a random
start and stop time within the configured leeway windows. The decision is
persisted to a state file so risk memory survives restarts.

Risk model:
  Each broadcast generates a risk score from weighted factors (start time,
  stop time, duration, day of week). Risk accumulates across days with
  exponential decay, so a risky broadcast last night still nudges today's
  probability toward caution but with diminishing influence over time.

  accumulated_risk = last_broadcast_risk + decay_factor × prev_accumulated_risk
  broadcast_probability = max(0, 1 − accumulated_risk)

The scheduler runs a background thread that sleeps until the next scheduled
event (decision, broadcast start, or broadcast stop) rather than polling on
a fixed interval.
"""

import json
import logging
import random
import threading

from datetime import datetime, timedelta
from pathlib import Path

from lpfm.config_loader import RiskConfig, SchedulerConfig
from lpfm.notifier import Notifier
from lpfm.relay_controller import RelayController, RelayError
from lpfm.stream_fetcher import StreamFetcher


class Scheduler:
    """Makes daily broadcast decisions and manages on-air timing.

    At decision_time each day, calculates accumulated risk, decides whether
    to broadcast, and if so picks random start/stop times within the leeway
    windows. Activates and deactivates the relay at the decided times.

    Args:
        scheduler_config: Window, leeway, and timing parameters from config.
        risk_config: Risk weights, decay factor, and day multipliers from config.
        relay_controller: Used to activate and deactivate the transmitter.
        stream_fetcher: Checked for stream health before activating transmitter.
        notifier: Optional Notifier for email alerts at decision time.
    """

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        risk_config: RiskConfig,
        relay_controller: RelayController,
        stream_fetcher: StreamFetcher,
        notifier: Notifier = None,
    ):
        self._scheduler_config = scheduler_config
        self._risk_config = risk_config
        self._relay_controller = relay_controller
        self._stream_fetcher = stream_fetcher
        self._notifier = notifier
        self._logger = logging.getLogger(__name__)

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._schedule_thread = None
        self._is_transmitting = False

    def start(self) -> None:
        """Start the background scheduling thread."""
        if self._schedule_thread and self._schedule_thread.is_alive():
            self._logger.warning("Scheduler start() called but already running")
            return
        self._stop_event.clear()
        self._schedule_thread = threading.Thread(
            target=self._schedule_loop,
            name="scheduler",
            daemon=True,
        )
        self._logger.info("Scheduler started")
        self._schedule_thread.start()

    def stop(self) -> None:
        """Signal the scheduling thread to stop and wait for it to exit."""
        if not self._schedule_thread or not self._schedule_thread.is_alive():
            return
        self._logger.info("Scheduler stopping")
        self._stop_event.set()
        self._wake_event.set()  # unblock any pending wait
        self._schedule_thread.join(timeout=10)

    def wake(self) -> None:
        """Wake the scheduler immediately to re-process state."""
        self._wake_event.set()

    def is_in_broadcast_window(self) -> bool:
        """Return True if the current time falls within the decided broadcast window.

        Used by the watchdog to determine whether to restore the transmitter
        after stream recovery. Handles midnight-crossing windows.
        """
        state = self._load_state()
        today = state.get("today", {})
        if not today.get("broadcasting"):
            return False
        now = datetime.now()
        start_dt, stop_dt = self._parse_broadcast_window(today)
        return start_dt <= now < stop_dt

    def is_emergency_shutoff(self) -> bool:
        """Return True if the emergency shutoff flag is active."""
        return bool(self._load_state().get("emergency_shutoff", False))

    @property
    def is_transmitting(self) -> bool:
        """Return True if the scheduler believes the transmitter is currently on."""
        return self._is_transmitting

    def transmitter_on(self) -> None:
        """Manually activate the transmitter regardless of schedule."""
        if not self._is_transmitting:
            self._activate_transmitter()

    def transmitter_off(self) -> None:
        """Manually deactivate the transmitter regardless of schedule."""
        if self._is_transmitting:
            self._deactivate_transmitter()

    # ── Schedule loop ─────────────────────────────────────────────────────────

    def _schedule_loop(self) -> None:
        """Main loop: determine the next action and sleep until it's time."""
        while not self._stop_event.is_set():
            sleep_seconds = self._process_schedule()
            # Clamp to at least 1 second to avoid tight loops on clock edge cases
            sleep_seconds = max(sleep_seconds, 1)
            self._logger.debug(f"Scheduler sleeping {sleep_seconds:.0f}s until next event")
            self._wake_event.wait(timeout=sleep_seconds)
            self._wake_event.clear()

    def _process_schedule(self) -> float:
        """Assess current schedule state and return seconds until the next event.

        Uses the state file's date — not today's calendar date — to determine
        when the next decision is due. This correctly handles midnight-crossing
        broadcast windows: after midnight we may still be inside the previous
        night's window, or waiting for the next decision_time later that morning.

        Returns:
            Seconds to sleep before calling this method again.
        """
        now = datetime.now()
        state = self._load_state()
        today_state = state.get("today", {})

        # ── Emergency shutoff overrides all scheduling ─────────────────────────
        if state.get("emergency_shutoff"):
            if self._is_transmitting:
                self._deactivate_transmitter()
            self._logger.warning("Emergency shutoff active — transmission suspended")
            return 60

        # ── No prior decision in state (first ever run) ───────────────────────
        if not today_state.get("decided"):
            decision_dt = self._today_at(self._scheduler_config.decision_time)
            if now >= decision_dt:
                self._make_daily_decision(now, state)
                return 60
            wait = (decision_dt - now).total_seconds()
            self._logger.info(
                f"Waiting {wait / 3600:.1f}h until decision time "
                f"({decision_dt.strftime('%H:%M')})"
            )
            return wait

        # Next decision is always one calendar day after the state's date
        state_date = datetime.strptime(today_state["date"], "%Y-%m-%d").date()
        next_decision_dt = datetime.combine(
            state_date + timedelta(days=1),
            self._scheduler_config.decision_time,
        )

        # ── Not broadcasting this period ──────────────────────────────────────
        if not today_state.get("broadcasting"):
            if now >= next_decision_dt:
                self._make_daily_decision(now, state)
                return 60
            wait = (next_decision_dt - now).total_seconds()
            self._logger.debug(f"Not broadcasting. Next decision in {wait / 3600:.1f}h")
            return wait

        # ── Broadcasting: parse window, handling midnight crossing ─────────────
        start_dt, stop_dt = self._parse_broadcast_window(today_state)

        if now < stop_dt:
            if now < start_dt:
                wait = (start_dt - now).total_seconds()
                self._logger.info(
                    f"Broadcast starts at {start_dt.strftime('%H:%M')} "
                    f"(in {wait / 60:.0f}min)"
                )
                return wait
            # On air
            if not self._is_transmitting:
                self._activate_transmitter()
            return (stop_dt - now).total_seconds()

        # Broadcast window has passed
        if self._is_transmitting:
            self._deactivate_transmitter()
        if now >= next_decision_dt:
            self._make_daily_decision(now, state)
            return 60
        wait = (next_decision_dt - now).total_seconds()
        self._logger.debug(f"Broadcast window ended. Next decision in {wait / 3600:.1f}h")
        return wait

    # ── Daily decision ────────────────────────────────────────────────────────

    def _make_daily_decision(self, now: datetime, state: dict) -> None:
        """Calculate risk, roll the dice, pick times, persist state, and notify.

        Args:
            now: Current datetime (used to resolve today's window times).
            state: Previously loaded state dict (contains yesterday's broadcast data).
        """
        today_str = now.strftime("%Y-%m-%d")

        # Update accumulated risk from yesterday's broadcast before deciding
        yesterday = state.get("today", {})
        yesterday_risk = (
            yesterday.get("risk_score", 0.0)
            if yesterday.get("broadcasting", False)
            else 0.0
        )
        accumulated_risk = (
            yesterday_risk + self._risk_config.decay_factor * state.get("accumulated_risk", 0.0)
        )
        accumulated_risk = max(0.0, min(1.0, accumulated_risk))

        # Calculate broadcast probability, applying hard threshold if configured
        threshold = self._risk_config.broadcast_threshold
        if threshold > 0 and accumulated_risk >= threshold:
            probability = 0.0
        else:
            probability = max(0.0, 1.0 - accumulated_risk)

        roll = random.random()
        broadcasting = roll < probability

        self._logger.info(
            f"Daily decision: accumulated_risk={accumulated_risk:.3f}, "
            f"probability={probability:.3f}, roll={roll:.3f}, "
            f"broadcasting={'YES' if broadcasting else 'NO'}"
        )

        if broadcasting:
            start_dt, stop_dt = self._pick_broadcast_times()
            risk_score = self._calculate_risk(start_dt, stop_dt)
            today_data = {
                "date": today_str,
                "decided": True,
                "broadcasting": True,
                "start": start_dt.strftime("%H:%M"),
                "stop": stop_dt.strftime("%H:%M"),
                "risk_score": risk_score,
            }
            self._logger.info(
                f"Broadcasting tonight {start_dt.strftime('%H:%M')}–"
                f"{stop_dt.strftime('%H:%M')} (risk: {risk_score:.3f})"
            )
            if self._notifier:
                self._notifier.send_broadcast_schedule(
                    start_dt, stop_dt, risk_score, accumulated_risk
                )
        else:
            today_data = {
                "date": today_str,
                "decided": True,
                "broadcasting": False,
                "risk_score": 0.0,
            }
            self._logger.info("Not broadcasting tonight")
            if self._notifier:
                self._notifier.send_no_broadcast_tonight(accumulated_risk)

        self._save_state({"accumulated_risk": accumulated_risk, "today": today_data})

    # ── Transmitter control ───────────────────────────────────────────────────

    def _activate_transmitter(self) -> None:
        """Turn the relay on if the stream is healthy.

        Applies a one-time stream URL override if one is set in today's state.
        """
        if not self._stream_fetcher.is_running():
            self._logger.warning(
                "Broadcast window start reached but stream is not running — "
                "holding off on transmitter until stream recovers"
            )
            return

        # Apply one-time stream URL override if configured for tonight
        override_url = self._load_state().get("today", {}).get("stream_url_override")
        if override_url:
            self._stream_fetcher.set_url(override_url)

        self._logger.info("Broadcast window start: activating transmitter")
        try:
            self._relay_controller.turn_on()
            self._is_transmitting = True
        except RelayError as e:
            self._logger.error(f"Failed to activate transmitter at window start: {e}")

    def _deactivate_transmitter(self) -> None:
        """Turn the relay off at the end of the broadcast window."""
        self._logger.info("Broadcast window end: deactivating transmitter")
        try:
            self._relay_controller.turn_off()
            self._is_transmitting = False
            self._stream_fetcher.reset_url()  # revert to default stream after broadcast
        except RelayError as e:
            self._logger.error(f"Failed to deactivate transmitter at window end: {e}")

    # ── Risk model ────────────────────────────────────────────────────────────

    def _calculate_risk(self, start_dt: datetime, stop_dt: datetime) -> float:
        """Calculate a 0–1 risk score for a broadcast at the given times.

        Risk factors:
          start_risk    — how close the start is to the window boundary (earlier = riskier)
          stop_risk     — how close the stop is to the window boundary (later = riskier)
          duration_risk — broadcast length as a fraction of the maximum possible
          day_risk      — per-day-of-week multiplier from config

        Weights are normalized so their configured values express relative importance
        rather than requiring them to sum to 1.0.

        Args:
            start_dt: Decided broadcast start datetime.
            stop_dt: Decided broadcast stop datetime.

        Returns:
            Risk score clamped to [0.0, 1.0].
        """
        window_start, window_end = self._window_datetimes()
        start_leeway = self._scheduler_config.start_leeway_max_minutes
        stop_leeway = self._scheduler_config.stop_leeway_max_minutes

        # Earlier start = higher risk (0 offset from window_start = max risk)
        start_offset_min = (start_dt - window_start).total_seconds() / 60
        start_risk = 1.0 - (start_offset_min / start_leeway) if start_leeway > 0 else 1.0

        # Later stop = higher risk (0 offset from window_end = max risk)
        stop_offset_min = (window_end - stop_dt).total_seconds() / 60
        stop_risk = 1.0 - (stop_offset_min / stop_leeway) if stop_leeway > 0 else 1.0

        # Longer duration = higher risk
        max_duration_min = (window_end - window_start).total_seconds() / 60
        duration_min = (stop_dt - start_dt).total_seconds() / 60
        duration_risk = duration_min / max_duration_min if max_duration_min > 0 else 0.0

        # Day-of-week risk from config
        day_name = start_dt.strftime("%A").lower()
        day_risk = self._risk_config.day_weights.get(day_name, 0.5)

        # Normalize weights so relative values drive risk, not their absolute sum
        w_start = self._risk_config.weight_start
        w_stop = self._risk_config.weight_stop
        w_duration = self._risk_config.weight_duration
        w_day = self._risk_config.weight_day
        total = w_start + w_stop + w_duration + w_day
        if total > 0:
            w_start /= total
            w_stop /= total
            w_duration /= total
            w_day /= total

        risk = (
            w_start * start_risk
            + w_stop * stop_risk
            + w_duration * duration_risk
            + w_day * day_risk
        )
        return max(0.0, min(1.0, risk))

    # ── Time picking ──────────────────────────────────────────────────────────

    def _pick_broadcast_times(self) -> tuple:
        """Randomly pick a start and stop time within the configured leeway windows.

        Start is picked in [window_start, window_start + start_leeway_max].
        Stop is picked in  [window_end - stop_leeway_max, window_end].

        Returns:
            Tuple of (start_datetime, stop_datetime).
        """
        window_start, window_end = self._window_datetimes()

        start_offset = random.randint(0, self._scheduler_config.start_leeway_max_minutes)
        stop_offset = random.randint(0, self._scheduler_config.stop_leeway_max_minutes)

        start_dt = window_start + timedelta(minutes=start_offset)
        stop_dt = window_end - timedelta(minutes=stop_offset)

        # Guard against inverted times from misconfigured leeway values
        if start_dt >= stop_dt:
            self._logger.warning(
                "Picked start/stop times were inverted — using window midpoint fallback"
            )
            mid = window_start + (window_end - window_start) / 2
            start_dt = mid - timedelta(minutes=30)
            stop_dt = mid + timedelta(minutes=30)

        return start_dt, stop_dt

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load the scheduler state from disk, returning empty dict if not found."""
        path = Path(self._scheduler_config.state_file)
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self._logger.error(f"Failed to load scheduler state: {e}")
            return {}

    def _save_state(self, state: dict) -> None:
        """Persist the scheduler state to disk and append a record to history.jsonl.

        Args:
            state: State dict to serialize as JSON.
        """
        path = Path(self._scheduler_config.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            self._logger.error(f"Failed to save scheduler state: {e}")
            return

        history_path = path.parent / "history.jsonl"
        entry = {"saved_at": datetime.now().isoformat(timespec="seconds"), **state}
        try:
            with open(history_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            self._logger.error(f"Failed to append to history: {e}")

    # ── Time helpers ──────────────────────────────────────────────────────────

    def _today_at(self, t) -> datetime:
        """Return today's date combined with the given time object."""
        now = datetime.now()
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    def _window_datetimes(self) -> tuple:
        """Return (window_start_dt, window_end_dt), adding a day to end if it crosses midnight."""
        start_dt = self._today_at(self._scheduler_config.window_start)
        end_dt = self._today_at(self._scheduler_config.window_end)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        return start_dt, end_dt

    def _parse_broadcast_window(self, today_state: dict) -> tuple:
        """Parse start/stop datetimes from state, handling midnight-crossing windows.

        Uses the state's date as the anchor for start; adds a day to stop if
        stop_time <= start_time (i.e. the window crosses midnight).

        Args:
            today_state: The 'today' sub-dict from the scheduler state file.

        Returns:
            Tuple of (start_datetime, stop_datetime).
        """
        state_date = datetime.strptime(today_state["date"], "%Y-%m-%d").date()
        start_t = datetime.strptime(today_state["start"], "%H:%M").time()
        stop_t = datetime.strptime(today_state["stop"], "%H:%M").time()
        start_dt = datetime.combine(state_date, start_t)
        stop_date = state_date + timedelta(days=1) if stop_t <= start_t else state_date
        stop_dt = datetime.combine(stop_date, stop_t)
        return start_dt, stop_dt

    def _parse_time_today(self, time_str: str) -> datetime:
        """Parse an HH:MM string and combine with today's date.

        Args:
            time_str: Time string in HH:MM format.
        """
        t = datetime.strptime(time_str, "%H:%M").time()
        return datetime.now().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
