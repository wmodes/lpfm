"""Notifier — sends email alerts about scheduled broadcasts.

Wraps Python's stdlib smtplib to send a notification at decision time when
the scheduler determines a broadcast will happen tonight. No external packages
required. SMTP credentials are loaded from .env via config_loader.

If notifications are disabled in config or credentials are missing, send
attempts are silently skipped — a notification failure should never interrupt
station operation.
"""

import logging
import smtplib

from datetime import datetime
from email.mime.text import MIMEText

from lpfm.config_loader import NotificationsConfig


class Notifier:
    """Sends email notifications about tonight's broadcast schedule.

    Args:
        notifications_config: SMTP credentials and enable flag from config.
    """

    def __init__(self, notifications_config: NotificationsConfig):
        self._config = notifications_config
        self._logger = logging.getLogger(__name__)

    def send_broadcast_schedule(
        self,
        start_time: datetime,
        stop_time: datetime,
        risk_score: float,
        accumulated_risk: float,
    ) -> None:
        """Send an email announcing tonight's broadcast start and stop times.

        Args:
            start_time: Decided broadcast start as a datetime.
            stop_time: Decided broadcast stop as a datetime.
            risk_score: Risk score calculated for tonight's broadcast (0–1).
            accumulated_risk: Current accumulated risk level (0–1).
        """
        if not self._config.enabled:
            return

        fmt = "%-I:%M %p"
        start_str = start_time.strftime(fmt)
        stop_str  = stop_time.strftime(fmt)
        subject = f"LPFM on air tonight: {start_str} – {stop_str}"
        body = (
            f"On air tonight: {start_str} – {stop_str}\n\n"
            f"  Start:  {start_str}\n"
            f"  Stop:   {stop_str}\n\n"
            f"  Tonight's risk:   {risk_score:.3f}\n"
            f"  Accumulated risk: {accumulated_risk:.3f}\n"
            f"  {self._risk_bar(accumulated_risk)}\n"
            f"  Broadcast probability: {max(0, int((1 - accumulated_risk) * 100))}%\n\n"
            f"Control panel:\n"
            f"  http://lpfm.local:8080\n"
            f"  http://localhost:8080\n\n"
            f"Remote access:\n"
            f"  https://connect.raspberrypi.com\n"
        )
        self._send_email(subject, body)

    def send_no_broadcast_tonight(self, accumulated_risk: float) -> None:
        """Send an email when the scheduler decides not to broadcast tonight.

        Args:
            accumulated_risk: Current accumulated risk level (0–1).
        """
        if not self._config.enabled:
            return

        subject = "LPFM off air tonight"
        body = (
            f"No broadcast tonight.\n\n"
            f"  Accumulated risk: {accumulated_risk:.3f}\n"
            f"  {self._risk_bar(accumulated_risk)}\n"
            f"  Broadcast probability: {max(0, int((1 - accumulated_risk) * 100))}%\n\n"
            f"Control panel:\n"
            f"  http://lpfm.local:8080\n"
            f"  http://localhost:8080\n\n"
            f"Remote access:\n"
            f"  https://connect.raspberrypi.com\n"
        )
        self._send_email(subject, body)

    def send_emergency_shutoff(self, accumulated_risk: float) -> None:
        """Send a notification that the station is under emergency shutoff.

        Args:
            accumulated_risk: Current accumulated risk level (0–1).
        """
        if not self._config.enabled:
            return

        subject = "LPFM emergency shutoff active"
        body = (
            f"The station is under emergency shutoff — no broadcast tonight.\n\n"
            f"  Accumulated risk: {accumulated_risk:.3f}\n"
            f"  {self._risk_bar(accumulated_risk)}\n\n"
            f"Restore transmission via the control panel:\n"
            f"  http://lpfm.local:8080\n"
            f"  http://localhost:8080\n\n"
            f"Remote access:\n"
            f"  https://connect.raspberrypi.com\n"
        )
        self._send_email(subject, body)

    def _risk_bar(self, risk: float, width: int = 24) -> str:
        """Render a text progress bar for the given risk value (0–1)."""
        filled = round(min(max(risk, 0.0), 1.0) * width)
        return '[' + '▓' * filled + '░' * (width - filled) + ']'

    def _send_email(self, subject: str, body: str) -> None:
        """Send an email via SMTP, logging and swallowing any errors.

        Station operation must never be interrupted by a notification failure.

        Args:
            subject: Email subject line.
            body: Plain-text email body.
        """
        if not self._credentials_present():
            self._logger.warning(
                "Notifications enabled but SMTP credentials missing — skipping"
            )
            return

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = self._config.smtp_user
        msg["To"] = self._config.notify_email

        try:
            with smtplib.SMTP(self._config.smtp_host, self._config.smtp_port) as smtp:
                smtp.starttls()
                smtp.login(self._config.smtp_user, self._config.smtp_password)
                smtp.send_message(msg)
            self._logger.info(f"Notification sent to {self._config.notify_email}: {subject}")
        except Exception as e:
            # Swallow all errors — notifications are best-effort
            self._logger.error(f"Failed to send notification: {e}")

    def _credentials_present(self) -> bool:
        """Return True if all required SMTP fields are populated."""
        return all([
            self._config.smtp_host,
            self._config.smtp_user,
            self._config.smtp_password,
            self._config.notify_email,
        ])
