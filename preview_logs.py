"""
Color preview for the logger.

Renders one representative line for every notification type through the real
ColoredFormatter, so what you see here is exactly what the bots print. Use it to
tune the hex palette in logger.py:

    python preview_logs.py

Edit the hex values in logger.py, rerun, repeat until you like it.
"""

import logging
import sys

# Console may be cp1250 (default Windows PL); force utf-8 before colorama (pulled
# in by importing logger) wraps stdout, so swatch glyphs encode cleanly.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import logger as L
from logger import ColoredFormatter, RESET

_FMT = ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s')


def _render(level: int, message: str) -> str:
    record = logging.LogRecord(
        name="SteamTradingLogger", level=level, pathname=__file__,
        lineno=0, msg=message, args=(), exc_info=None)
    return _FMT.format(record)


# (level, message) — real-shaped lines pulled from trade_bot.py / bidding_bot.py.
# Grouped by tag. INFO lines get the tag color; WARNING/ERROR get the level color
# (yellow/red) regardless of tag — same as production.
SAMPLES: list[tuple[int, str]] = [
    # --- bidding bot ---------------------------------------------------------
    (logging.INFO,    "[ws] connecting..."),
    (logging.INFO,    "[ws] connected"),
    (logging.INFO,    "[ws] authenticated as kamil"),
    (logging.INFO,    "[ws] disconnected — reconnecting in 5s"),
    (logging.ERROR,   "[ws] init failed: connection refused"),

    (logging.INFO,    "[filters] updated — balance 124.50 C"),
    (logging.INFO,    "[filters] balance too low to bid: 0.12 C"),

    (logging.INFO,    "[auction] 884213 AK-47 | Redline (FT) — market 12.40 C / max 15.00 C"),
    (logging.INFO,    "[auction] none active"),
    (logging.ERROR,   "[auction] fetch active failed: HTTP 503"),

    (logging.INFO,    "[outbid] 884213 AK-47 | Redline (FT): 12.40 -> 12.53 C"),

    (logging.INFO,    "[bid] 884213 placed 12.53 C"),
    (logging.INFO,    "[bid] 884213 re-bidding at 12.66 C"),
    (logging.ERROR,   "[bid] 884213 rejected — This offer was already placed by someone else!"),
    (logging.ERROR,   "[bid] 884213 failed — server error"),
    (logging.ERROR,   "[bid] 884213 gave up — 'one trade at a time' persisted"),

    (logging.INFO,    "[trade] status 5 — AK-47 | Redline (FT) (id 884213)"),
    (logging.INFO,    "[recv] AK-47 | Redline (FT) 12.53 C from seller (tradeId 884213, steamId 7656...)"),

    (logging.INFO,    "[skip] Gallery Case — stale price (74 min old)"),

    # --- trade bot -----------------------------------------------------------
    (logging.INFO,    "[token] Steam access token refreshed on Empire"),
    (logging.ERROR,   "[token] update failed: {'success': False}"),

    (logging.INFO,    "[offers] checking for new trade offers..."),
    (logging.INFO,    "[offers] 2 unmatched offer(s)"),
    (logging.ERROR,   "[offers] fetch withdrawals failed: timeout"),

    (logging.INFO,    "[accept] steam offer 9164300268 from dear basketball (7656...) — Revolution Case accepted"),
    (logging.ERROR,   "[accept] steam offer 9164300268 (Revolution Case from dear basketball) failed: 401"),

    (logging.INFO,    "[recv] Revolution Case (empire item 12345, empire offer 359775786) marked as received"),
    (logging.WARNING, "[recv] Revolution Case (steam offer 9164300268) not found on Empire (mark-as-received)"),
    (logging.ERROR,   "[recv] Revolution Case (empire item 12345) mark-as-received failed: 500"),

    (logging.INFO,    "[decline] offer 9164305165 from 7656... — wants items from us"),

    (logging.INFO,    "[dispute] Dreams & Nightmares Case — value 1.20 C, bought 2026-06-15 21:10:00, seller ROGÉRIU (7656...)"),
    (logging.INFO,    "[dispute] skip 359775786 — already recorded as purchased (mark-received likely failed earlier)"),
    (logging.ERROR,   "[dispute] Gallery Case (empire item 12345) failed on CSGOEmpire: 409"),

    (logging.ERROR,   "[db] write failed for accepted trade 884213: locked"),

    (logging.WARNING, "[telegram] not sent: 429 Too Many Requests"),
    (logging.ERROR,   "[telegram] retry failed: 429 Too Many Requests"),

    # --- untagged / level fallbacks -----------------------------------------
    (logging.DEBUG,    "raw debug line without a tag"),
    (logging.CRITICAL, "critical line without a tag"),
]


def print_palette() -> None:
    """Swatch of every palette constant so hex -> appearance is obvious."""
    names = ["RED", "GREEN", "YELLOW", "BLUE", "MAGENTA",
             "CYAN", "CYAN_DARK", "WHITE", "GREY", "GREY_DARK"]
    print("PALETTE (logger.py constants)")
    print("-" * 50)
    for name in names:
        code = getattr(L, name, None)
        if code is None:
            continue
        print(f"  {code}{name:<11}{RESET} {code}████████████{RESET}")
    print()


def print_samples() -> None:
    print("NOTIFICATION TYPES")
    print("-" * 50)
    last_tag = None
    for level, message in SAMPLES:
        tag = message.split("]", 1)[0].lstrip("[") if message.startswith("[") else "(none)"
        if tag != last_tag:
            print()  # blank line between tag groups
            last_tag = tag
        print(_render(level, message))
    print()


if __name__ == "__main__":
    print()
    print_palette()
    print_samples()
