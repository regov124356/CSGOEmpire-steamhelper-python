"""
Pricing layer for the CSFloat side.

Owns everything CSFloatClient deliberately does not: the price math, the divider
conversion between CSFloat (USD cents) and Empire coins, the decision of what a
missing market means, and the continuous refresh loop over the tracked items.

Throughput is bounded by CSFloat's per-endpoint limits (/listings = 200/h is the
binding one). The client's adaptive limiter paces requests automatically, so the
loop just round-robins the items continuously; with ~45 items each is refreshed
roughly every ~14 minutes.
"""

import asyncio
import math
from typing import Optional
from urllib.parse import quote

from logger import logger
from db import DB
from csfloatclient import CSFloatClient, CSFloatError

CSFLOAT_FEE = 0.98  # CSFloat takes a 2% fee
CYCLE_PAUSE = 5     # breather between full round-robin passes
ERROR_BACKOFF = 60  # longer wait when an entire pass failed (CSFloat down / 403 storm)


class PriceService:
    def __init__(self, client: CSFloatClient, db: DB, divider: float):
        self.client = client
        self.db = db
        self.divider = divider

    # ------------------------------------------------------------------ #
    # price computation
    # ------------------------------------------------------------------ #
    async def check_price(self, market_hash_name: str) -> Optional[tuple[int, int]]:
        """Return (empire_price, float_price) in cents/coincents, or None if the
        item currently has no usable market (no listings or no plain buy orders).

        Raises CSFloatError on a fetch failure so the caller can keep the old
        price instead of overwriting it.
        """
        if self.divider is None:
            raise ValueError("divider not set")

        name = quote(market_hash_name, safe="")
        # get_all_listings returns {"listings": [...], "cursor": ...} (csfloat_api
        # >=1.1.0), not a bare list as in 1.0.2.
        result = await self.client.get_all_listings(
            sort_by="lowest_price", type_="buy_now", market_hash_name=name)
        listings = result["listings"]
        if not listings:
            return None

        listing_price = listings[0].price

        buy_orders = await self.client.get_buy_orders(listing_id=listings[0].id)
        unconditional = [bo for bo in (buy_orders or []) if not bo.expression]
        if not unconditional:
            return None

        buy_order_price = unconditional[0].price
        float_price = math.floor(
            buy_order_price if listing_price < 100 else (listing_price + buy_order_price) / 2)
        empire_price = math.floor(math.floor(float_price * CSFLOAT_FEE) / self.divider)
        return empire_price, float_price

    # ------------------------------------------------------------------ #
    # refresh loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        while True:
            try:
                rows = await self.db.check_item_prices()
                if not rows:
                    logger.warning("No items to price; retrying in 60s.")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"Refreshing prices for {len(rows)} items...")
                errors = 0
                for item_id, market_hash_name in rows:
                    if not await self._refresh_one(item_id, market_hash_name):
                        errors += 1

                # Whole pass failed (CSFloat outage / 403 storm) -> back off so we
                # don't tight-loop hammering the API; otherwise a short breather.
                if errors == len(rows):
                    logger.warning(f"All {errors} refreshes failed; backing off {ERROR_BACKOFF}s.")
                    await asyncio.sleep(ERROR_BACKOFF)
                else:
                    await asyncio.sleep(CYCLE_PAUSE)
            except Exception as e:
                logger.exception(f"Price loop error: {e}")
                await asyncio.sleep(ERROR_BACKOFF)

    async def _refresh_one(self, item_id: int, market_hash_name: str) -> bool:
        """Refresh one item. Returns False only on a fetch/DB error (a missing
        market is not an error), so the loop can detect a full-pass outage."""
        try:
            result = await self.check_price(market_hash_name)
            if result is None:
                logger.info(f"No market data for {market_hash_name}; keeping old price.")
                return True
            empire_price, float_price = result
            await self.db.update_items_prices(
                id=item_id, price_empire=empire_price, price_float=float_price)
            return True
        except CSFloatError as e:
            logger.error(f"Price fetch failed for {market_hash_name}: {e}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected error pricing {market_hash_name}: {e}")
            return False
