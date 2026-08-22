import logging


def configure_logging(level: int = logging.INFO) -> None:
    """Configure simple structured-enough logging for local services."""

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
