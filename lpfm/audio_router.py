"""Audio router — sets output volume on the USB audio device at startup.

Locates the ALSA card by searching for a configurable name in `aplay -l`
output rather than relying on a hardcoded card number, which can shift if
USB devices are added or removed. Only runs on ALSA (Pi); skips silently
on other platforms so macOS dev works unchanged.
"""

import logging
import re
import subprocess
from typing import Optional

from lpfm.config_loader import AudioConfig


class AudioRouter:
    """Configures the audio output device at station startup.

    Args:
        audio_config: Audio format, device, and volume settings from config.
    """

    def __init__(self, audio_config: AudioConfig):
        self._config = audio_config
        self._logger = logging.getLogger(__name__)

    def configure(self) -> None:
        """Set the output volume on the ALSA audio device.

        No-op on non-ALSA platforms (e.g. macOS audiotoolbox).
        """
        if self._config.format != "alsa":
            return

        card = self._find_alsa_card()
        if card is None:
            self._logger.warning(
                f"Could not find ALSA card matching '{self._config.device_name}' "
                f"— skipping volume configuration"
            )
            return

        self._set_volume(card, self._config.output_volume)

    def _find_alsa_card(self) -> Optional[int]:
        """Search aplay -l output for a card whose name contains device_name.

        Returns:
            Card number as an integer, or None if not found.
        """
        try:
            result = subprocess.run(
                ["aplay", "-l"], capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._logger.error(f"Failed to enumerate ALSA cards: {e}")
            return None

        for line in result.stdout.splitlines():
            if self._config.device_name.lower() in line.lower():
                match = re.match(r"card (\d+):", line)
                if match:
                    return int(match.group(1))

        return None

    def _set_volume(self, card: int, volume: int) -> None:
        """Set output volume on the given ALSA card.

        Tries each known mixer control name in order, stopping at the first
        that succeeds. Different devices expose different control names
        (e.g. USB dongles use 'Speaker'; the Pi headphone jack uses 'PCM Playback Volume').

        Args:
            card: ALSA card number.
            volume: Volume percent 0–100.
        """
        controls = ["Speaker", "Headphone Playback Volume", "PCM Playback Volume", "Master"]
        for control in controls:
            try:
                subprocess.run(
                    ["amixer", "-c", str(card), "sset", control, f"{volume}%"],
                    capture_output=True,
                    check=True,
                )
                self._logger.info(
                    f"Audio output volume set to {volume}% "
                    f"(card {card}: {self._config.device_name}, control: {control})"
                )
                return
            except subprocess.CalledProcessError:
                continue
            except OSError as e:
                self._logger.error(f"Failed to set audio volume: {e}")
                return
        self._logger.warning(
            f"No recognized mixer control found on card {card} — volume not set"
        )
