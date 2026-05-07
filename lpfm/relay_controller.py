"""Relay controller — sends on/off commands to the Shelly wifi power relay.

Controls transmitter power via HTTP GET requests to the Shelly 1 Mini Gen4
local RPC API. Stateless — each call is a single request with no persistent
connection. Called by the scheduler at broadcast window boundaries and by
the watchdog when stream failure requires an emergency shutoff.

The Shelly RPC endpoints used:
  Switch.Set?id=0&on=true   — turn relay on
  Switch.Set?id=0&on=false  — turn relay off
  Switch.GetStatus?id=0     — query current relay state
"""

import logging
import time

import requests

from lpfm.config_loader import RelayConfig


class RelayError(Exception):
    """Raised when a relay command fails or the relay is unreachable."""
    pass


class RelayController:
    """Controls the Shelly wifi power relay over the local network.

    Sends HTTP GET requests to the Shelly RPC API to switch the transmitter
    on or off, and to query the current relay state.

    Args:
        relay_config: Relay connection parameters from config.
    """

    # Timeout for all relay HTTP requests (seconds)
    REQUEST_TIMEOUT = 5

    def __init__(self, relay_config: RelayConfig):
        self._config = relay_config
        self._logger = logging.getLogger(__name__)

    def turn_on(self) -> None:
        """Switch the relay on and verify it reached the on state.

        Sends the on command then confirms the relay reports on, retrying
        up to verify_retries times before raising RelayError.

        Raises:
            RelayError: If the command fails or the relay doesn't reach the on state.
        """
        self._logger.info("Relay: turning ON (transmitter power on)")
        self._send_and_verify(self._config.on_path, expected_state=True)

    def turn_off(self) -> None:
        """Switch the relay off and verify it reached the off state.

        Sends the off command then confirms the relay reports off, retrying
        up to verify_retries times before raising RelayError.

        Raises:
            RelayError: If the command fails or the relay doesn't reach the off state.
        """
        self._logger.info("Relay: turning OFF (transmitter power off)")
        self._send_and_verify(self._config.off_path, expected_state=False)

    def get_state(self) -> bool:
        """Query the current relay state.

        Returns:
            True if the relay is on (transmitter powered), False if off.

        Raises:
            RelayError: If the request fails or the response is unreadable.
        """
        url = self._config.url + self._config.status_path
        self._logger.debug(f"Relay: querying state at {url}")
        try:
            response = requests.get(url, timeout=self.REQUEST_TIMEOUT)
            response.raise_for_status()
            # Shelly GetStatus response: {"id": 0, "output": true, ...}
            state = response.json().get("output", False)
            self._logger.debug(f"Relay: current state is {'ON' if state else 'OFF'}")
            return bool(state)
        except requests.RequestException as e:
            raise RelayError(f"Failed to query relay state: {e}") from e
        except (ValueError, KeyError) as e:
            raise RelayError(f"Unexpected relay status response: {e}") from e

    def _send_and_verify(self, path: str, expected_state: bool) -> None:
        """Send a command and retry until the relay reaches the expected state.

        Sends the command, then polls get_state() up to verify_retries times,
        waiting verify_delay_seconds between each check. Raises RelayError if
        the relay has not reached the expected state after all retries.

        Args:
            path: The RPC path to call.
            expected_state: True if we expect the relay to be on, False for off.

        Raises:
            RelayError: If the command fails or the expected state is not reached.
        """
        label = "ON" if expected_state else "OFF"
        for attempt in range(1, self._config.verify_retries + 1):
            self._send(path)
            time.sleep(self._config.verify_delay_seconds)
            if self.get_state() == expected_state:
                self._logger.info(f"Relay: confirmed {label} (attempt {attempt})")
                return
            self._logger.warning(
                f"Relay: expected {label} but state mismatch — "
                f"retry {attempt}/{self._config.verify_retries}"
            )
        raise RelayError(
            f"Relay failed to reach {label} state after {self._config.verify_retries} attempts"
        )

    def _send(self, path: str) -> None:
        """Send a GET request to the relay at the given path.

        Args:
            path: The RPC path to call (e.g. /rpc/Switch.Set?id=0&on=true).

        Raises:
            RelayError: If the request fails or returns a non-2xx status.
        """
        url = self._config.url + path
        self._logger.debug(f"Relay: GET {url}")
        try:
            response = requests.get(url, timeout=self.REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            raise RelayError(f"Relay command failed ({url}): {e}") from e
