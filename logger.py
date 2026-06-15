import logging
import os
import re
from datetime import datetime

try:
    import colorama
    colorama.init()
except ImportError:
    pass

RESET   = '\033[0m'
BOLD    = '\033[1m'
DIM     = '\033[2m'
RED     = '\033[91m'
GREEN   = '\033[92m'
YELLOW  = '\033[93m'
BLUE    = '\033[94m'
MAGENTA = '\033[95m'
CYAN    = '\033[96m'
WHITE   = '\033[97m'

# tag -> color (applied to the whole line when level is INFO/DEBUG)
_TAG_COLORS: dict[str, str] = {
    "bid":      CYAN,
    "outbid":   YELLOW,
    "auction":  CYAN,
    "filters":  BLUE,
    "ws":       DIM + WHITE,
    "trade":    MAGENTA,
    "recv":     GREEN,
    "accept":   GREEN,
    "offers":   BLUE,
    "decline":  YELLOW,
    "dispute":  YELLOW,
    "token":    DIM + WHITE,
    "skip":     DIM,
    "db":       MAGENTA,
    "telegram": BLUE,
}

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG:    DIM,
    logging.INFO:     WHITE,
    logging.WARNING:  YELLOW,
    logging.ERROR:    RED,
    logging.CRITICAL: BOLD + RED,
}

_TAG_RE = re.compile(r'\[(\w+)\]')


class ColoredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)

        if record.levelno >= logging.WARNING:
            color = _LEVEL_COLORS.get(record.levelno, WHITE)
            return f"{color}{line}{RESET}"

        msg = record.getMessage()
        m = _TAG_RE.search(msg)
        if m:
            tag = m.group(1).lower()
            color = _TAG_COLORS.get(tag, WHITE)
        else:
            color = _LEVEL_COLORS.get(record.levelno, WHITE)

        return f"{color}{line}{RESET}"


class Logger:
    def __init__(self, log_dir="logs"):
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        current_date = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(log_dir, f"steam_trading_{current_date}.log")

        self.logger = logging.getLogger("SteamTradingLogger")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            plain_fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            color_fmt = ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s')

            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(plain_fmt)

            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(color_fmt)

            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

    def debug(self, message):
        self.logger.debug(message)

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)

    def exception(self, message):
        self.logger.exception(message)

logger = Logger()
