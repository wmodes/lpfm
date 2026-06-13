"""Stream fetcher — connects to the remote audio stream and routes it to the audio output.

Wraps ffmpeg as a managed subprocess. ffmpeg handles the HTTP Icecast connection,
audio decoding, and ALSA output. Brief dropouts are recovered automatically by
ffmpeg's built-in reconnect flags. Sustained failures are surfaced via is_running()
for the watchdog to act on.

External dependency: ffmpeg must be installed as a system package.
  Raspberry Pi:  sudo apt install ffmpeg
  macOS dev:     brew install ffmpeg
"""

import logging
import shutil
import subprocess
import threading

from lpfm.config_loader import AudioConfig, StreamConfig


class StreamFetcherError(Exception):
    """Raised when the stream fetcher cannot start or encounters a fatal error."""
    pass


class StreamFetcher:
    """Manages the ffmpeg subprocess that fetches and plays the audio stream.

    Connects to the configured Icecast stream URL and outputs decoded PCM audio
    directly to the configured ALSA device. ffmpeg is run with reconnect flags
    so brief network dropouts are handled transparently.

    Args:
        stream_config: Stream connection parameters from config.
        audio_config: Audio output device parameters from config.
    """

    def __init__(self, stream_config: StreamConfig, audio_config: AudioConfig):
        self._stream_config = stream_config
        self._audio_config = audio_config
        self._process = None
        self._stderr_thread = None
        self._url_override = None
        self._stopping = False   # set True during intentional stop to suppress false exit alarms
        self._logger = logging.getLogger(__name__)

    def start(self) -> None:
        """Launch the ffmpeg subprocess and begin streaming audio.

        Raises:
            StreamFetcherError: If ffmpeg is not found or the process fails to start.
        """
        if self.is_running():
            self._logger.warning("start() called but stream fetcher is already running")
            return

        if not shutil.which("ffmpeg"):
            raise StreamFetcherError(
                "ffmpeg not found — install it with: sudo apt install ffmpeg (Pi) "
                "or brew install ffmpeg (macOS)"
            )

        cmd = self._build_command()
        url = self._url_override or self._stream_config.url
        self._logger.info(f"Starting stream: {url}")
        self._logger.debug(f"ffmpeg command: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            raise StreamFetcherError(f"Failed to launch ffmpeg: {e}") from e

        # Drain ffmpeg stderr in a background thread — without this, the pipe
        # buffer fills and ffmpeg blocks.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
        )
        self._stderr_thread.start()

    def stop(self) -> None:
        """Terminate the ffmpeg subprocess.

        Sends SIGTERM and waits up to 5 seconds for a clean exit before
        escalating to SIGKILL.
        """
        if not self.is_running():
            return

        self._logger.info("Stopping stream fetcher")
        self._stopping = True
        self._process.terminate()

        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._logger.warning("ffmpeg did not exit cleanly; sending SIGKILL")
            self._process.kill()
            self._process.wait()

        self._process = None
        self._stopping = False

    def is_running(self) -> bool:
        """Return True if the ffmpeg subprocess is currently alive."""
        return self._process is not None and self._process.poll() is None

    def restart(self) -> None:
        """Stop and restart the ffmpeg subprocess."""
        self._logger.info("Restarting stream fetcher")
        self.stop()
        self.start()

    def set_url(self, url: str) -> None:
        """Restart the stream with a one-time URL override.

        Args:
            url: Stream URL to use for this broadcast session.
        """
        self._logger.info(f"Stream URL override: {url}")
        self._url_override = url
        if self.is_running():
            self.stop()
            self.start()

    def reset_url(self) -> None:
        """Clear any URL override and restart with the configured default."""
        if self._url_override is not None:
            self._logger.info("Clearing stream URL override — reverting to default")
            self._url_override = None
            if self.is_running():
                self.stop()
                self.start()

    def _build_command(self) -> list:
        """Construct the ffmpeg command from current config."""
        return [
            "ffmpeg",
            "-loglevel", "warning",        # suppress noisy info/debug output
            "-reconnect", "1",             # reconnect on disconnect
            "-reconnect_streamed", "1",    # reconnect on streamed sources
            "-reconnect_delay_max", str(self._stream_config.retry_delay_seconds),
            "-i", self._url_override or self._stream_config.url,
            "-vn",                         # discard any video stream
            "-acodec", "pcm_s16le",        # decode to raw 16-bit PCM for ALSA
            "-ar", "44100",                # sample rate
            "-ac", "2",                    # stereo
            "-f", self._audio_config.format,
            self._audio_config.device,
        ]

    def _drain_stderr(self) -> None:
        """Read ffmpeg stderr line by line and forward to the Python logger.

        Runs in a daemon thread for the lifetime of the subprocess. ffmpeg
        writes warnings and errors to stderr; log them at WARNING so they
        surface without enabling full DEBUG output.
        """
        process = self._process
        for raw_line in process.stderr:
            line = raw_line.decode(errors="replace").rstrip()
            if line:
                self._logger.warning(f"ffmpeg: {line}")
        # stderr EOF means the process exited — log the return code unless we stopped it
        if process and process.poll() is not None and not self._stopping:
            rc = process.returncode
            if rc == 0:
                self._logger.info(f"ffmpeg exited cleanly (rc={rc})")
            else:
                self._logger.error(f"ffmpeg exited unexpectedly (rc={rc})")
