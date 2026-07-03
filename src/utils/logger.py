import sys
from pathlib import Path

from loguru import logger


def setup_logger(log_dir: Path = Path("data"), level: str = "DEBUG") -> None:
    """Configure Loguru with console (INFO+) and rotating file (DEBUG+) sinks.

    File logs are written to data/bot.log and rotated at 10 MB.
    Console output uses colour and a compact format.
    """
    logger.remove()

    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    log_file = log_dir / "bot.log"
    logger.add(
        str(log_file),
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} — {message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
    )

    logger.info(f"Logger initialised — file sink: {log_file}")
