"""Fallback player — plays local audio when the remote stream is unavailable.

Scans a directory of audio files, shuffles them, and plays them one at a time
via ffmpeg. When the list is exhausted it reshuffles and repeats, looping
indefinitely until stop() is called. Activated by the watchdog on stream
failure and deactivated on recovery.

If the fallback directory is missing or empty, start() logs an error and
returns without raising — silence is preferable to a crash during an
already-degraded state.
"""

import logging
import random
import subprocess
import threading

from pathlib import Path

from lpfm.config_loader import AudioConfig, FallbackConfig


class FallbackPlayer:
    """Plays shuffled local audio files as a fallback when the stream is down.

    Manages a background playback thread that works through a shuffled list of
    audio files, reshuffling and repeating when the list is exhausted. Each file
    is played via a short-lived ffmpeg subprocess.

    Args:
        fallback_config: Fallback directory and file extension settings from config.
        audio_config: Audio output device parameters from config (shared with StreamFetcher).
    """

    def __init__(self, fallback_config: FallbackConfig, audio_config: AudioConfig):
        self._fallback_config = fallback_config
        self._audio_config = audio_config
        self._logger = logging.getLogger(__name__)

        self._stop_event = threading.Event()
        self._playback_thread = None
        self._current_process = None

    def start(self) -> None:
        """Scan the fallback directory and begin shuffled playback in a background thread."""
        if self.is_running():
            self._logger.warning("FallbackPlayer start() called but already running")
            return

        files = self._scan_directory()
        if not files:
            # Logged inside _scan_directory — return silently rather than raising
            return

        self._stop_event.clear()
        self._playback_thread = threading.Thread(
            target=self._playback_loop,
            args=(files,),
            name="fallback-player",
            daemon=True,
        )
        self._logger.info(f"Fallback player started ({len(files)} files)")
        self._playback_thread.start()

    def stop(self) -> None:
        """Stop playback, terminating the current file and the playback thread."""
        if not self.is_running():
            return

        self._logger.info("Fallback player stopping")
        self._stop_event.set()

        # Terminate the current ffmpeg subprocess so the thread unblocks immediately
        if self._current_process and self._current_process.poll() is None:
            self._current_process.terminate()
            self._current_process.wait()

        self._playback_thread.join(timeout=5)

    def is_running(self) -> bool:
        """Return True if the playback thread is active."""
        return self._playback_thread is not None and self._playback_thread.is_alive()

    # ── Playback loop ─────────────────────────────────────────────────────────

    def _playback_loop(self, files: list) -> None:
        """Work through the file list, reshuffling and repeating until stopped.

        Args:
            files: Initial list of audio file paths to play.
        """
        playlist = list(files)
        random.shuffle(playlist)
        index = 0

        while not self._stop_event.is_set():
            if index >= len(playlist):
                # List exhausted — reshuffle and repeat
                random.shuffle(playlist)
                index = 0

            self._play_file(playlist[index])
            index += 1

    def _play_file(self, path: str) -> None:
        """Play a single audio file via ffmpeg, blocking until it finishes or stop is requested.

        Args:
            path: Absolute path to the audio file.
        """
        cmd = self._build_command(path)
        self._logger.debug(f"Fallback: playing {Path(path).name}")

        try:
            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            self._logger.error(f"Failed to launch ffmpeg for fallback file {path}: {e}")
            return

        # Wait for the file to finish, waking every 0.5s to check for stop signal
        while not self._stop_event.is_set():
            try:
                self._current_process.wait(timeout=0.5)
                break  # file finished normally
            except subprocess.TimeoutExpired:
                continue  # still playing

        # Clean up if stop was requested mid-file
        if self._stop_event.is_set() and self._current_process.poll() is None:
            self._current_process.terminate()
            self._current_process.wait()

        self._current_process = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _scan_directory(self) -> list:
        """Scan the fallback directory and return a list of matching audio file paths.

        Returns:
            List of absolute path strings for all matching files. Empty list if
            the directory is missing or contains no matching files.
        """
        dir_path = Path(self._fallback_config.audio_dir)

        if not dir_path.is_dir():
            self._logger.error(f"Fallback audio directory not found: {dir_path}")
            return []

        files = []
        for ext in self._fallback_config.file_extensions:
            files.extend(dir_path.glob(f"*.{ext}"))
            files.extend(dir_path.glob(f"*.{ext.upper()}"))

        if not files:
            self._logger.error(f"No audio files found in fallback directory: {dir_path}")
            return []

        return [str(f) for f in files]

    def _build_command(self, path: str) -> list:
        """Construct the ffmpeg command to play a single file to the audio output device.

        Args:
            path: Path to the audio file to play.
        """
        return [
            "ffmpeg",
            "-loglevel", "warning",
            "-i", path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            "-f", self._audio_config.format,
            self._audio_config.device,
        ]
