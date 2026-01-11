"""Logging helper"""
from loguru import logger
from src.config import LOG_DIR

LOG_FILE = LOG_DIR / "xhs_mcp_{time}.log"


def configure_logging() -> None:
    logger.remove()
    logger.add(
        LOG_FILE,
        rotation="20 MB",
        retention="14 days",
        enqueue=True,
        level="INFO",
    )
    logger.add(lambda msg: print(msg, end=""), level="INFO")


__all__ = ["configure_logging", "logger"]
