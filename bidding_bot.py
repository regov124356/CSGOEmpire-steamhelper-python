"""
Realtime auction bidder for CSGOEmpire.

Listens to the CSGOEmpire trade websocket and bids on auctions whose price is at
or below our own valuation (price_empire in the DB, produced by PriceService from
CSFloat resale value). Winning below our valuation is the margin.

This is the buying counterpart to TradeBot (which accepts the resulting trades).
It runs as its own process via run_bidder.py.

REST (place bid / list auctions / metadata) goes through CSGOEmpireClient; only
the websocket transport and the bidding strategy live here.
"""

import asyncio
import time
from datetime import datetime, timezone
from enum import Enum

import socketio

from logger import logger
from db import DB
from csgoempire_client import CSGOEmpireClient, CSGOEmpireError

# Websocket lives on a different host than the REST API.
WS_URL = "wss://trade.csgoempire.com"
WS_PATH = "/s/"
NAMESPACE = "/trade"

MIN_BALANCE = 30           # coincents; below this we can't meaningfully bid
PRICE_MAX_ABOVE = 20       # only surface auctions <= 20% above market value
# Don't trust a price older than this (PriceService stalled / process down) —
# bidding on a stale valuation risks overpaying. price_updated_at is naive UTC.
PRICE_MAX_AGE = 60 * 60
PER_PAGE = 2500
RECONNECT_DELAY = 5
INIT_RETRY_DELAY = 180
# Cap on retries while Empire reports "one trade at a time" (1s apart).
MAX_ONE_TRADE_RETRIES = 10
# CSGOEmpire's websocket pushes no balance event, so balance is cached from
# metadata and only refetched at most once per this window (coalesces the burst
# of trade_status events that all signal the same balance change).
BALANCE_TTL = 5.0

# Empire error messages we react to specifically.
ERR_ONE_TRADE = "You can only make one trade at a time. Please wait a moment and try again."
ERR_NO_BALANCE = ("You don't have enough coins to do that!",
                  "You don't have enough balance to do that!")
ERR_OUTBID = ("This item has already been bid on for a higher amount",
              "This offer was already placed by someone else!")


class BidResult(Enum):
    SUCCESS = 1
    FAILED = 0
    NO_BALANCE = 2


class BiddingBot:
    def __init__(self, empire: CSGOEmpireClient, db: DB):
        self.empire = empire
        self.db = db
        self._sio = socketio.AsyncClient(ssl_verify=False, reconnection=False)
        self._auctions_lock = asyncio.Lock()
        # Empire processes one trade at a time, so serialise our bids instead of
        # firing them concurrently and fighting "one trade at a time" errors.
        self._bid_lock = asyncio.Lock()
        self._active_auctions: dict[int, str] = {}  # auction id -> market_name
        self._meta: dict | None = None
        self._balance: int = 0          # cached, coins*100
        self._last_refresh: float = 0.0  # monotonic time of last metadata fetch
        self._register_handlers()

    @property
    def _user(self) -> dict:
        return self._meta["user"]

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        while True:
            try:
                await self._connect_and_wait()
            except Exception as e:
                logger.error(f"[ws] init failed: {e}")
                await asyncio.sleep(INIT_RETRY_DELAY)
                continue
            logger.info(f"[ws] disconnected — reconnecting in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_wait(self) -> None:
        # Fresh metadata each connection -> fresh socket token/signature/balance.
        await self._fetch_metadata()
        logger.info("[ws] connecting...")
        await self._sio.connect(
            WS_URL, namespaces=[NAMESPACE], socketio_path=WS_PATH,
            transports=["websocket"],
            headers={"User-Agent": f"{self._user['id']} API Bot"})
        await self._sio.wait()  # returns on disconnect

    def _register_handlers(self) -> None:
        self._sio.on("connect", self._on_connect, namespace=NAMESPACE)
        self._sio.on("init", self._on_init, namespace=NAMESPACE)
        self._sio.on("new_item", self._on_new_item, namespace=NAMESPACE)
        self._sio.on("auction_update", self._on_auction_update, namespace=NAMESPACE)
        self._sio.on("trade_status", self._on_trade_status, namespace=NAMESPACE)
        self._sio.on("disconnect", self._on_disconnect, namespace=NAMESPACE)

    # ------------------------------------------------------------------ #
    # connection / auth events
    # ------------------------------------------------------------------ #
    async def _on_connect(self) -> None:
        logger.info("[ws] connected")

    async def _on_init(self, data) -> None:
        if data and data.get("authenticated"):
            logger.info(f"[ws] authenticated as {data.get('name')}")
            await self._update_filters()
        else:
            await self._sio.emit("identify", {
                "uid": self._user["id"],
                "model": self._user,
                "authorizationToken": self._meta["socket_token"],
                "signature": self._meta["socket_signature"],
            }, namespace=NAMESPACE)

    async def _on_disconnect(self) -> None:
        logger.info("[ws] socket disconnected")

    async def _fetch_metadata(self) -> None:
        """Refetch metadata and refresh the cached balance."""
        self._meta = await self.empire.get_metadata()
        self._balance = self._user.get("balance", 0)
        self._last_refresh = time.monotonic()

    async def _update_filters(self) -> None:
        if self._balance < MIN_BALANCE:
            logger.info(f"[filters] balance too low to bid: {self._balance / 100:.2f} C")
            return
        await self._sio.emit("filters", {
            "price_max": self._balance,
            "per_page": PER_PAGE,
            "auction": "yes",
            "price_max_above": PRICE_MAX_ABOVE,
        }, namespace=NAMESPACE)
        logger.info(f"[filters] updated — balance {self._balance / 100:.2f} C")

    async def _refresh_user_and_filters(self) -> None:
        # Coalesce bursts: skip the REST refetch if we just refreshed; the cached
        # balance is good enough until the TTL elapses.
        if time.monotonic() - self._last_refresh >= BALANCE_TTL:
            await self._fetch_metadata()
        await self._update_filters()

    # ------------------------------------------------------------------ #
    # auction events
    # ------------------------------------------------------------------ #
    async def _on_new_item(self, items) -> None:
        await asyncio.gather(*(self._consider_new_item(i) for i in items))

    async def _consider_new_item(self, item) -> None:
        item_id = item.get("id")
        market_name = item.get("market_name")
        market_value = item.get("market_value")
        if item_id is None or market_name is None or market_value is None:
            return

        bid_max = await self._fresh_bid_max(market_name)
        if bid_max is None:
            return
        if bid_max < market_value:
            return
        # Skip what we can't afford; otherwise Empire rejects every attempt with
        # "not enough coins" and the auction_update stream re-triggers it in a loop.
        if market_value > self._balance:
            return

        logger.info(f"[auction] {item_id} {market_name} — "
                    f"market {market_value / 100:.2f} C / max {bid_max / 100:.2f} C")
        result = await self._place_bid(item_id, int(market_value), bid_max)
        if result is BidResult.SUCCESS:
            await self._get_active_auctions()
        elif result is BidResult.NO_BALANCE:
            await self._refresh_user_and_filters()

    async def _on_auction_update(self, items) -> None:
        await asyncio.gather(*(self._consider_auction_update(i) for i in items))

    async def _consider_auction_update(self, item) -> None:
        item_id = item.get("id")
        highest = item.get("auction_highest_bid")
        if item_id is None or highest is None:
            return

        market_name = await self._market_name_for(item_id)
        if not market_name or item.get("auction_highest_bidder") == self._user["id"]:
            return

        bid_max = await self._fresh_bid_max(market_name)
        if bid_max is None:
            return

        # Empire requires exactly 1% above (rounded half away from zero); the
        # client helper implements that rule so the bid isn't rejected.
        bid = self.empire.min_next_bid(highest)
        if bid_max < bid:
            return
        # Skip what we can't afford (see _consider_new_item) — stops the futile
        # outbid loop while others keep raising an auction past our balance.
        if bid > self._balance:
            return

        logger.info(f"[outbid] {item_id} {market_name}: {highest / 100:.2f} -> {bid / 100:.2f} C")
        # bid_max=0: the exact bid is already capped above, so disable escalation.
        result = await self._place_bid(item_id, bid, bid_max=0)
        if result is BidResult.NO_BALANCE:
            await self._refresh_user_and_filters()

    async def _on_trade_status(self, items) -> None:
        needs_refresh = False
        for item in items:
            data = item.get("data") or {}
            status = data.get("status")
            if status is None:
                continue
            market_name = data.get("item", {}).get("market_name")
            logger.info(f"[trade] status {status} — {market_name} (id {data.get('id')})")

            if item.get("type") != "withdrawal":
                continue
            if status == 5:
                partner = data.get("metadata", {}).get("partner", {})
                logger.info(f"[recv] {market_name} {data.get('total_value', 0) / 100:.2f} C "
                            f"from {partner.get('steam_name')} "
                            f"(tradeId {data.get('id')}, steamId {partner.get('steam_id')})")
            if status == 13:
                logger.info(f"[recv] {market_name} (id {data.get('id')})")
            if status in (4, 8, 9, 10, 11):
                needs_refresh = True

        if needs_refresh:
            await self._refresh_user_and_filters()

    # ------------------------------------------------------------------ #
    # bidding
    # ------------------------------------------------------------------ #
    async def _place_bid(self, item_id, bid_value: int, bid_max: int) -> BidResult:
        """Place a bid, escalating up to bid_max when outbid. Serialised by the
        bid lock so only one bid is in flight at a time (Empire requirement)."""
        async with self._bid_lock:
            one_trade_retries = 0
            while True:
                try:
                    await self.empire.place_bid(item_id, bid_value, fail_fast_429=True)
                    logger.info(f"[bid] {item_id} placed {bid_value / 100:.2f} C")
                    return BidResult.SUCCESS
                except CSGOEmpireError as err:
                    payload = err.payload if isinstance(err.payload, dict) else {}
                    message = payload.get("message", "")

                    if err.status and err.status >= 500:
                        logger.error(f"[bid] {item_id} failed — server error")
                        return BidResult.FAILED

                    logger.error(f"[bid] {item_id} rejected — {message or err}")

                    if message == ERR_ONE_TRADE:
                        one_trade_retries += 1
                        if one_trade_retries > MAX_ONE_TRADE_RETRIES:
                            logger.error(f"[bid] {item_id} gave up — 'one trade at a time' persisted")
                            return BidResult.FAILED
                        await asyncio.sleep(1)
                        continue

                    if any(m in message for m in ERR_OUTBID):
                        next_bid = payload.get("data", {}).get("next_bid")
                        if next_bid is not None and int(next_bid) <= bid_max:
                            bid_value = int(next_bid)
                            logger.info(f"[bid] {item_id} re-bidding at {bid_value / 100:.2f} C")
                            continue
                        return BidResult.FAILED

                    if any(m in message for m in ERR_NO_BALANCE):
                        return BidResult.NO_BALANCE

                    return BidResult.FAILED

    async def _get_active_auctions(self) -> None:
        try:
            resp = await self.empire.get_active_auctions()
        except CSGOEmpireError as err:
            logger.error(f"[auction] fetch active failed: {err}")
            return
        auctions = resp.get("active_auctions") or []
        async with self._auctions_lock:
            self._active_auctions = {a["id"]: a["market_name"] for a in auctions}
        if not auctions:
            logger.info("[auction] none active")

    async def _market_name_for(self, item_id) -> str | None:
        async with self._auctions_lock:
            return self._active_auctions.get(item_id)

    async def _fresh_bid_max(self, market_name: str) -> int | None:
        """Our max bid for an item, or None if it isn't priced or the price is
        stale. A stale/NULL price_updated_at means PriceService isn't keeping up,
        so we refuse to bid on a valuation we can't trust."""
        row = await self.db.get_item_price(market_name)
        if row is None:
            return None
        price, updated_at = row
        if updated_at is None:
            logger.info(f"[skip] {market_name} — no price timestamp")
            return None
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, matches DB
        age = (now_utc - updated_at).total_seconds()
        if age > PRICE_MAX_AGE:
            logger.info(f"[skip] {market_name} — stale price ({age / 60:.0f} min old)")
            return None
        return int(price)
