import argparse
import asyncio
import os
import signal

from config_loader import load_config, select_user
from csfloatclient import CSFloatClient
from csgoempire_client import CSGOEmpireClient
from db import DB

from bidding_bot import BiddingBot
from logger import logger
from price_service import PriceService
from steam_client import SteamClient
from telegram import Telegram
from trade_bot import TradeBot

# When the bidder is stopped (Ctrl+C), TradeBot keeps running this long so it can
# still receive a late trade offer — a seller has up to 30 min to send, plus slack.
# Overridable via env (mainly for testing the drain without waiting 33 min).
DRAIN_AFTER_BIDDER_STOP = int(os.environ.get("BIDDER_DRAIN_SECONDS", 33 * 60))


async def run_all(run_bidder: bool):
    data = load_config()
    user = select_user(data["users"])

    api_key_float = user["float"]["api_key"]
    api_key_steam = user["steam"]["api_key"]
    username = user["steam"]["username"]
    password = user["steam"]["password"]
    steam_guard_path = user["steam"].get("steam_guard_path")

    bearer_auth = user["empire"]["api_key"]

    t_token = data["telegram"]["token"]
    t_chat_id = data["telegram"]["chat_id"]
    telegram = Telegram(t_token, t_chat_id)

    db_user = data["db"]["user"]
    db_password = data["db"]["password"]
    db_database = data["db"]["database"]
    db_host = data["db"]["host"]

    divider = float(data["divider"])

    db = DB(user=db_user, password=db_password, host=db_host, database=db_database)

    steam_client = SteamClient(api_key=api_key_steam, username=username, password=password,
                               steam_guard=steam_guard_path)

    # One CSGOEmpireClient for TradeBot and BiddingBot -> one shared rate-limit
    # window (bids get priority over polling inside it).
    async with CSGOEmpireClient(bearer_auth) as empire, \
            CSFloatClient(api_key_float) as csfloat:
        bot = TradeBot(steam=steam_client, empire=empire, db=db,
                       telegram=telegram, divider=divider)
        price_service = PriceService(client=csfloat, db=db, divider=divider)

        tasks = [
            asyncio.ensure_future(bot.run()),
            asyncio.ensure_future(price_service.run()),
        ]

        if not run_bidder:
            await asyncio.gather(*tasks)
            return

        bidder = BiddingBot(empire=empire, db=db)
        bidder_task = asyncio.ensure_future(bidder.run())
        tasks.append(bidder_task)

        await _run_with_graceful_bidder(bidder, tasks)


async def _run_with_graceful_bidder(bidder: BiddingBot, tasks: list) -> None:
    """Run until Ctrl+C. First Ctrl+C stops the bidder and keeps TradeBot
    running for DRAIN_AFTER_BIDDER_STOP; a second Ctrl+C quits immediately.

    Uses signal.signal (not loop.add_signal_handler, which is unavailable on
    Windows) so the same code path works on every platform. SIGBREAK (Ctrl+Break,
    Windows only) is treated the same as SIGINT.
    """
    loop = asyncio.get_running_loop()
    stop_bidder = asyncio.Event()
    force_quit = asyncio.Event()
    sigint_count = 0

    def _on_sigint(_signum, _frame):
        nonlocal sigint_count
        sigint_count += 1
        loop.call_soon_threadsafe(
            stop_bidder.set if sigint_count == 1 else force_quit.set)

    prev_int = signal.signal(signal.SIGINT, _on_sigint)
    prev_break = (signal.signal(signal.SIGBREAK, _on_sigint)
                  if hasattr(signal, "SIGBREAK") else None)
    stop_wait = asyncio.ensure_future(stop_bidder.wait())
    try:
        # Wake on the first Ctrl+C, or if any task dies on its own.
        await asyncio.wait([stop_wait, *tasks], return_when=asyncio.FIRST_COMPLETED)

        if stop_bidder.is_set():
            logger.info(
                f"[shutdown] Ctrl+C — stopping bidder; TradeBot drains for "
                f"{DRAIN_AFTER_BIDDER_STOP // 60} min "
                f"(Ctrl+C again to quit now)")
            await bidder.stop()
            try:
                await asyncio.wait_for(force_quit.wait(), DRAIN_AFTER_BIDDER_STOP)
                logger.info("[shutdown] second Ctrl+C — quitting now")
            except asyncio.TimeoutError:
                logger.info("[shutdown] drain window elapsed — quitting")
    finally:
        signal.signal(signal.SIGINT, prev_int)
        if prev_break is not None:
            signal.signal(signal.SIGBREAK, prev_break)
        stop_wait.cancel()
        for t in tasks:
            t.cancel()
        results = await asyncio.gather(*tasks, stop_wait, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.error(f"[shutdown] task ended with error: {r!r}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SteamTrading bot")
    parser.add_argument(
        "--bidder", action="store_true",
        help="also run the auction bidder (requires TradeBot; shares its rate limit)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(run_all(args.bidder))
