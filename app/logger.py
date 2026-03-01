import logging
import uuid
from datetime import datetime

logger = logging.getLogger("chili")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | trace=%(trace_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]

def log_info(trace_id: str, message: str):
    logger.info(message, extra={"trace_id": trace_id})