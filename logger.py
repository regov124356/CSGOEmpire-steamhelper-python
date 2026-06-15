import logging
import os
import re
from datetime import datetime

try:
    import colorama
    # just_fix_windows_console enables the console's native VT processing without
    # wrapping stdout. colorama.init()'s convert mode only understands 16-color
    # codes and STRIPS 24-bit truecolor (38;2;r;g;b) — which is all we emit.
    if hasattr(colorama, "just_fix_windows_console"):
        colorama.just_fix_windows_console()
    else:
        colorama.init(convert=False, strip=False)  # don't mangle truecolor
except ImportError:
    pass

RESET = '\033[0m'
BOLD  = '\033[1m'


def _fg(hex_code: str) -> str:
    """Hex (#rrggbb) -> 24-bit truecolor ANSI foreground escape."""
    h = hex_code.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'\033[38;2;{r};{g};{b}m'


# ---- palette: edit these hex values to taste -------------------------------
# Pick any hex (e.g. from coolors.co). Truecolor needs a modern terminal
# (Windows Terminal ok). Greys go light -> dark: GREY > GREY_DARK.
RED     = _fg("#f56666")
GREEN   = _fg("#2c852c")
GREEN_DARK = _fg("#185818")
YELLOW  = _fg('#d7af5f')
BLUE    = _fg("#4969aa")
MAGENTA = _fg('#af5fd7')
CYAN    = _fg("#28a1a1")
CYAN_DARK = _fg("#176868")
WHITE   = _fg('#e4e4e4')   # default INFO line
GREY      = _fg("#B9B9B9")  # light grey — low-signal housekeeping
GREY_DARK = _fg("#2B2B2B")  # dark grey  — connection noise / DEBUG
# ----------------------------------------------------------------------------

# tag -> color (applied to the whole line when level is INFO/DEBUG).
# A few hues carry the signal; everything else is greyscale plumbing so the eye
# lands on what matters.
#   GREEN   — value landed   (item received / offer accepted)
#   CYAN    — live auction    (bid / new auction / outbid)
#   YELLOW  — needs attention (dispute / decline)
# Greyscale (GREY > GREY_DARK): trade/db records + connection housekeeping.
_TAG_COLORS: dict[str, str] = {
    "bid":      GREY,
    "accept":   GREEN_DARK,
    "recv":     GREEN,
    "auction":  CYAN,
    "outbid":   GREY,
    "dispute":  YELLOW,
    "decline":  YELLOW,
    "trade":    GREY,
    "db":       GREY,
    "offers":   GREY,
    "filters":  BLUE,
    "telegram": GREY,
    "ws":       GREY_DARK,
    "token":    BLUE,
    "skip":     GREY_DARK,
}

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG:    GREY_DARK,
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
