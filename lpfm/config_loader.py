"""Config loader — parses and validates config/config.toml at startup.

All runtime parameters are defined in config/config.toml. This module loads
that file, validates that all required fields are present, and returns a
typed Config object that is passed to all components at construction time.
A ConfigError is raised immediately on any missing or invalid field — the
system never starts with an incomplete configuration.
"""

import os

import tomli
from dataclasses import dataclass
from datetime import time
from dotenv import load_dotenv
from pathlib import Path
from typing import List


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or invalid."""
    pass


@dataclass
class StreamConfig:
    """Parameters for connecting to and buffering the remote audio stream."""
    url: str
    buffer_seconds: int
    retry_limit: int
    retry_delay_seconds: int


@dataclass
class BroadcastWindow:
    """A single time window during which the transmitter may be active."""
    start: time
    end: time


@dataclass
class BroadcastConfig:
    """Scheduling rules that define when the station is on air."""
    windows: List[BroadcastWindow]


@dataclass
class RelayConfig:
    """Connection details for the wifi power relay controlling the transmitter."""
    url: str
    on_path: str
    off_path: str
    status_path: str
    verify_retries: int
    verify_delay_seconds: int


@dataclass
class SchedulerConfig:
    """Broadcast window and daily decision timing."""
    decision_time: time
    window_start: time
    window_end: time
    start_leeway_max_minutes: int
    stop_leeway_max_minutes: int
    state_file: str


@dataclass
class RiskConfig:
    """Risk model weights, decay, and per-day multipliers."""
    decay_factor: float
    broadcast_threshold: float
    weight_start: float
    weight_stop: float
    weight_duration: float
    weight_day: float
    day_weights: dict   # {day_name: float} — keyed by lowercase day name


@dataclass
class NotificationsConfig:
    """Email notification settings for broadcast schedule alerts."""
    enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    notify_email: str


@dataclass
class WatchdogConfig:
    """Tuning parameters for stream health monitoring and recovery."""
    poll_interval_seconds: int
    restart_attempts: int
    restart_cooldown_seconds: int


@dataclass
class AudioConfig:
    """Audio output device and format configuration."""
    format: str         # ffmpeg output format: "alsa" on Pi, "audiotoolbox" on macOS
    device: str         # output device name passed to ffmpeg
    device_name: str    # human-readable name used to locate the ALSA card (e.g. "USB Audio")
    output_volume: int  # output volume percent 0–100; applied via amixer on ALSA only


@dataclass
class FallbackConfig:
    """Local audio source used when the remote stream is unavailable."""
    audio_dir: str
    file_extensions: List[str]


@dataclass
class LoggingConfig:
    """Logging verbosity for all station components."""
    level: str


@dataclass
class Config:
    """Top-level config object passed to all station components."""
    stream: StreamConfig
    audio: AudioConfig
    scheduler: SchedulerConfig
    risk: RiskConfig
    notifications: NotificationsConfig
    broadcast: BroadcastConfig
    relay: RelayConfig
    watchdog: WatchdogConfig
    fallback: FallbackConfig
    logging: LoggingConfig


class ConfigLoader:
    """Loads and validates the station config from a TOML file.

    Usage:
        config = ConfigLoader().load("config/config.toml")
    """

    def load(self, path: str) -> Config:
        """Parse and validate the config file at the given path.

        Loads .env from the project root before parsing, so environment
        variables are available to override machine-specific or sensitive
        fields that are intentionally left blank in config.toml.

        Args:
            path: Path to the TOML config file.

        Returns:
            A fully validated Config object.

        Raises:
            ConfigError: If the file is missing, unreadable, or any required
                field is absent or invalid.
        """
        load_dotenv()
        raw = self._read_file(path)
        return Config(
            stream=self._parse_stream(raw),
            audio=self._parse_audio(raw),
            scheduler=self._parse_scheduler(raw),
            risk=self._parse_risk(raw),
            notifications=self._parse_notifications(raw),
            broadcast=self._parse_broadcast(raw),
            relay=self._parse_relay(raw),
            watchdog=self._parse_watchdog(raw),
            fallback=self._parse_fallback(raw),
            logging=self._parse_logging(raw),
        )

    def _read_file(self, path: str) -> dict:
        """Read and parse the TOML file, raising ConfigError on any failure."""
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            with open(config_path, "rb") as f:
                return tomli.load(f)
        except tomli.TOMLDecodeError as e:
            raise ConfigError(f"Failed to parse config file: {e}") from e

    def _require(self, section: dict, key: str, section_name: str):
        """Return a required field, raising ConfigError if it is absent."""
        if key not in section:
            raise ConfigError(f"Missing required config field: [{section_name}].{key}")
        return section[key]

    def _env(self, var: str, fallback: str) -> str:
        """Return the env var value if set and non-empty, otherwise the fallback.

        Raises ConfigError if both the env var and fallback are empty — this
        means a required value was not provided in .env or config.toml.
        """
        value = os.environ.get(var, "").strip() or fallback.strip()
        if not value:
            raise ConfigError(
                f"Missing required value: set {var} in .env or provide a value in config.toml"
            )
        return value

    def _require_section(self, raw: dict, section_name: str) -> dict:
        """Return a required config section, raising ConfigError if absent."""
        if section_name not in raw:
            raise ConfigError(f"Missing required config section: [{section_name}]")
        return raw[section_name]

    def _parse_stream(self, raw: dict) -> StreamConfig:
        s = self._require_section(raw, "stream")
        return StreamConfig(
            url=self._env("LPFM_STREAM_URL", s.get("url", "")),
            buffer_seconds=self._require(s, "buffer_seconds", "stream"),
            retry_limit=self._require(s, "retry_limit", "stream"),
            retry_delay_seconds=self._require(s, "retry_delay_seconds", "stream"),
        )

    def _parse_broadcast(self, raw: dict) -> BroadcastConfig:
        s = self._require_section(raw, "broadcast")
        raw_windows = self._require(s, "windows", "broadcast")
        windows = []
        for i, w in enumerate(raw_windows):
            try:
                start = time.fromisoformat(w["start"])
                end = time.fromisoformat(w["end"])
            except (KeyError, ValueError) as e:
                raise ConfigError(
                    f"Invalid broadcast window at index {i}: {e}"
                ) from e
            windows.append(BroadcastWindow(start=start, end=end))
        return BroadcastConfig(windows=windows)

    def _parse_relay(self, raw: dict) -> RelayConfig:
        s = self._require_section(raw, "relay")
        return RelayConfig(
            url=self._env("LPFM_RELAY_URL", s.get("url", "")),
            on_path=self._env("LPFM_RELAY_ON_PATH", s.get("on_path", "")),
            off_path=self._env("LPFM_RELAY_OFF_PATH", s.get("off_path", "")),
            status_path=self._env("LPFM_RELAY_STATUS_PATH", s.get("status_path", "")),
            verify_retries=self._require(s, "verify_retries", "relay"),
            verify_delay_seconds=self._require(s, "verify_delay_seconds", "relay"),
        )

    def _parse_audio(self, raw: dict) -> AudioConfig:
        s = self._require_section(raw, "audio")
        return AudioConfig(
            format=self._env("LPFM_AUDIO_FORMAT", s.get("format", "")),
            device=self._env("LPFM_AUDIO_DEVICE", s.get("device", "")),
            device_name=self._require(s, "device_name", "audio"),
            output_volume=self._require(s, "output_volume", "audio"),
        )

    def _parse_scheduler(self, raw: dict) -> SchedulerConfig:
        s = self._require_section(raw, "scheduler")
        try:
            return SchedulerConfig(
                decision_time=time.fromisoformat(self._require(s, "decision_time", "scheduler")),
                window_start=time.fromisoformat(self._require(s, "window_start", "scheduler")),
                window_end=time.fromisoformat(self._require(s, "window_end", "scheduler")),
                start_leeway_max_minutes=self._require(s, "start_leeway_max_minutes", "scheduler"),
                stop_leeway_max_minutes=self._require(s, "stop_leeway_max_minutes", "scheduler"),
                state_file=self._require(s, "state_file", "scheduler"),
            )
        except ValueError as e:
            raise ConfigError(f"Invalid time format in [scheduler]: {e}") from e

    def _parse_risk(self, raw: dict) -> RiskConfig:
        s = self._require_section(raw, "risk")
        return RiskConfig(
            decay_factor=self._require(s, "decay_factor", "risk"),
            broadcast_threshold=self._require(s, "broadcast_threshold", "risk"),
            weight_start=self._require(s, "weight_start", "risk"),
            weight_stop=self._require(s, "weight_stop", "risk"),
            weight_duration=self._require(s, "weight_duration", "risk"),
            weight_day=self._require(s, "weight_day", "risk"),
            day_weights=s.get("day_weights", {}),
        )

    def _parse_notifications(self, raw: dict) -> NotificationsConfig:
        s = self._require_section(raw, "notifications")
        return NotificationsConfig(
            enabled=self._require(s, "enabled", "notifications"),
            # SMTP fields are sensitive — live in .env, not config.toml
            smtp_host=os.environ.get("LPFM_SMTP_HOST", ""),
            smtp_port=int(os.environ.get("LPFM_SMTP_PORT", "587")),
            smtp_user=os.environ.get("LPFM_SMTP_USER", ""),
            smtp_password=os.environ.get("LPFM_SMTP_PASSWORD", ""),
            notify_email=os.environ.get("LPFM_NOTIFY_EMAIL", ""),
        )

    def _parse_watchdog(self, raw: dict) -> WatchdogConfig:
        s = self._require_section(raw, "watchdog")
        return WatchdogConfig(
            poll_interval_seconds=self._require(s, "poll_interval_seconds", "watchdog"),
            restart_attempts=self._require(s, "restart_attempts", "watchdog"),
            restart_cooldown_seconds=self._require(s, "restart_cooldown_seconds", "watchdog"),
        )

    def _parse_fallback(self, raw: dict) -> FallbackConfig:
        s = self._require_section(raw, "fallback")
        return FallbackConfig(
            audio_dir=self._require(s, "audio_dir", "fallback"),
            file_extensions=self._require(s, "file_extensions", "fallback"),
        )

    def _parse_logging(self, raw: dict) -> LoggingConfig:
        s = self._require_section(raw, "logging")
        return LoggingConfig(
            level=self._require(s, "level", "logging"),
        )
