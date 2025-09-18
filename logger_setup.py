import logging
import os
import time
from logging.handlers import RotatingFileHandler
import colorlog
import re

# -------------------------------------------------------------------
# Default log formats
# -------------------------------------------------------------------
# Plain file format
_DEF_FMT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
# Colored console format
_DEF_COLOR_FMT = "%(log_color)s%(asctime)s | %(levelname)-8s | %(name)s |%(reset)s %(message)s"

# Level-to-color mapping for console output
_COLORS = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold_red",
}

# -------------------------------------------------------------------
# Secret masking (avoid leaking credentials in logs)
# -------------------------------------------------------------------
# Regex to mask password part in URLs like scheme://user:pass@host
_PASS_IN_URL = re.compile(r"(\w[\w+.-]*://[^:@/]+:)([^@\s]+)(@)")

def _mask_secrets(text: str) -> str:
    if not isinstance(text, str):
        return text
    # mysql+pymysql://user:pass@host → mysql+pymysql://user:****@host
    text = _PASS_IN_URL.sub(r"\1****\3", text)
    return text

class _SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Mask strings in message and arguments; keep shapes unchanged
            if isinstance(record.msg, str):
                record.msg = _mask_secrets(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_mask_secrets(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: (_mask_secrets(v) if isinstance(v, str) else v) for k, v in record.args.items()}
        except Exception:
            # Best-effort; never block logging on masking errors
            pass
        return True

# -------------------------------------------------------------------
# Fixed-offset time zone for log timestamps (e.g., UTC+3)
# -------------------------------------------------------------------
# Use a fixed offset independent of system clock/time zone
_TZ_OFFSET_HOURS = int(os.getenv("APP_TZ_OFFSET_HOURS", "3"))
_TZ_OFFSET_SECONDS = _TZ_OFFSET_HOURS * 3600

def _tz_converter(epoch_seconds: float):
    """
    Hook used by logging formatters to convert epoch to struct_time
    using a fixed offset. Ensures consistent timestamps across hosts.
    """
    return time.gmtime(epoch_seconds + _TZ_OFFSET_SECONDS)

# -------------------------------------------------------------------
# Logging configuration (console + rotating file)
# -------------------------------------------------------------------
def configure_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    """
    Configure logging to both console (colored) and a rotating file.
    - log_dir: directory for log files
    - level: minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """

    # Ensure log directory exists
    os.makedirs(log_dir, exist_ok=True)
    logfile = os.path.join(log_dir, "wpbot.log")

    # Root logger
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Secret masking filter (applied to all handlers + root)
    secret_filter = _SecretFilter()

    # ---------------- Console handler (colored) ----------------
    ch = colorlog.StreamHandler()
    ch.setLevel(level.upper())
    ch_fmt = colorlog.ColoredFormatter(_DEF_COLOR_FMT, log_colors=_COLORS)
    ch_fmt.converter = _tz_converter  # fixed-offset timestamps
    ch.setFormatter(ch_fmt)
    ch.addFilter(secret_filter)

    # ---------------- Rotating file handler ----------------
    # Rolls at ~2 MB, keeps 5 backups
    fh = RotatingFileHandler(
        logfile,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8"
    )
    fh.setLevel(level.upper())
    fh_fmt = logging.Formatter(_DEF_FMT)
    fh_fmt.converter = _tz_converter  # fixed-offset timestamps
    fh.setFormatter(fh_fmt)
    fh.addFilter(secret_filter)

    # Replace any existing handlers with our two handlers
    root.handlers = []
    root.addHandler(ch)
    root.addHandler(fh)
    # Also attach the filter to root in case of future handlers
    root.addFilter(secret_filter)
