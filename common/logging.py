import logging

from common.config import get_settings


def setup_logging(service: str) -> logging.Logger:
    logging.basicConfig(
        level=get_settings().log_level,
        format=f"%(asctime)s %(levelname)s [{service}] %(name)s: %(message)s",
    )
    return logging.getLogger(service)
