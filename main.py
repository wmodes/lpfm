"""Entry point — loads config and wires all components together."""

import logging
import signal
import sys

from lpfm.config_loader import ConfigLoader, ConfigError
from lpfm.fallback_player import FallbackPlayer
from lpfm.notifier import Notifier
from lpfm.relay_controller import RelayController
from lpfm.scheduler import Scheduler
from lpfm.stream_fetcher import StreamFetcher
from lpfm.watchdog import Watchdog


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def main() -> None:
    try:
        config = ConfigLoader().load("config/config.toml")
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.logging.level)
    logger = logging.getLogger(__name__)
    logger.info("LPFM station starting")

    # Build components in dependency order
    notifier = Notifier(config.notifications)
    relay = RelayController(config.relay)
    fallback = FallbackPlayer(config.fallback, config.audio)
    stream = StreamFetcher(config.stream, config.audio)
    scheduler = Scheduler(config.scheduler, config.risk, relay, stream, notifier)
    watchdog = Watchdog(config.watchdog, stream, relay, fallback, scheduler)

    def shutdown(signum, frame):
        logger.info("Shutdown signal received — stopping station")
        watchdog.stop()
        scheduler.stop()
        stream.stop()
        fallback.stop()
        logger.info("LPFM station stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    stream.start()
    scheduler.start()
    watchdog.start()

    logger.info("LPFM station running")
    signal.pause()


if __name__ == "__main__":
    main()
