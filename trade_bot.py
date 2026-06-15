"""
Orchestration layer for the CSGOEmpire buying bot.

Ties together three components, each with a single responsibility:
    - SteamClient        : Steam operations (accept/decline offers, tokens)
    - CSGOEmpireClient   : CSGOEmpire HTTP API
    - DB / Telegram      : persistence and notifications

TradeBot owns the business logic and the long-running loops.
"""

import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

from logger import logger
from db import DB
from telegram import Telegram
from steam_client import SteamClient
from price_service import CSFLOAT_FEE
from csgoempire_client import CSGOEmpireClient, CSGOEmpireError, TradeStatus

# Withdrawals in these states still need our attention.
ACTIVE_WITHDRAWAL_STATUSES = (TradeStatus.SENT, TradeStatus.DISPUTED)
# Dispute a withdrawal once it has less than this many seconds left to live.
DISPUTE_THRESHOLD_SECONDS = 540


class TradeBot:
    def __init__(self, steam: SteamClient, empire: CSGOEmpireClient, db: DB,
                 telegram: Telegram, divider: float):
        self.steam = steam
        self.empire = empire
        self.db = db
        self.telegram = telegram
        self.divider = divider

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def usteamid_to_commid(accountid) -> str:
        """Convert a Steam account id to a 64-bit community id."""
        return str(76561197960265728 + accountid)

    @staticmethod
    def current_date_str() -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def from_empire_to_float_converter(self, price_empire: float) -> float:
        return round((price_empire * self.divider) / CSFLOAT_FEE, 2)

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        await asyncio.gather(
            self.update_automation_loop(),
            self.check_new_offers_loop(),
        )

    # ------------------------------------------------------------------ #
    # loop: keep the Steam access token fresh on Empire
    # ------------------------------------------------------------------ #
    async def update_automation_loop(self) -> None:
        while True:
            try:
                status = await self.empire.get_automation_status()
                if status.get('success'):
                    data = status.get('data', {})

                    if not data.get('has_access_token', False):
                        token = await asyncio.to_thread(self.steam.get_access_token)
                        result = await self.empire.update_access_token(token)
                        if not result.get('success'):
                            logger.error(f"[token] update failed: {result}")
                        await asyncio.sleep(60)
                        continue

                    expires_at = data.get('access_token_expires_at')
                    if expires_at:
                        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                        wait = (expiry - datetime.now(timezone.utc)).total_seconds()
                        if wait > 0:
                            await asyncio.sleep(wait + 1)
                            continue

                await asyncio.sleep(60)
            except Exception as e:
                logger.exception(f"[token] automation loop error: {e}")
                await asyncio.sleep(60)

    # ------------------------------------------------------------------ #
    # loop: match incoming Steam offers to Empire withdrawals
    # ------------------------------------------------------------------ #
    async def check_new_offers_loop(self) -> None:
        while True:
            logger.info("[offers] checking for new trade offers...")
            try:
                offers_empire = await self.get_active_withdrawals()
                if offers_empire:
                    offers_steam = await asyncio.to_thread(
                        self.steam.get_trade_offers, use_webtoken=True)
                    await self.accept_matching_offers(offers_empire, offers_steam)
            except Exception as e:
                logger.exception(f"[offers] check failed: {e}")
            finally:
                await asyncio.sleep(60 + random.randint(0, 120))

    async def get_active_withdrawals(self) -> list:
        try:
            response = await self.empire.get_active_trades()
        except CSGOEmpireError as e:
            logger.error(f"[offers] fetch withdrawals failed: {e}")
            return []

        withdrawals = response.get("data", {}).get("withdrawals", [])
        return [w for w in withdrawals if w.get("status") in ACTIVE_WITHDRAWAL_STATUSES]

    # ------------------------------------------------------------------ #
    # core matching logic
    # ------------------------------------------------------------------ #
    async def accept_matching_offers(self, offers_empire: list, offers_steam: dict) -> None:
        offers_steam = offers_steam.get("response", {}).get("trade_offers_received", [])
        tasks = []

        for oe in offers_empire[:]:
            oe_steamid64 = oe.get("metadata", {}).get("partner", {}).get("steam_id")
            oe_market_name = oe.get("item", {}).get("market_name")
            oe_item_id = oe.get("item_id")
            expires_at = oe.get("metadata", {}).get("expires_at")
            oe_expires_soon = (expires_at is not None and
                               int(expires_at) - int(time.time()) < DISPUTE_THRESHOLD_SECONDS)
            oe_created_at = oe.get("created_at")
            oe_total_value = oe.get("total_value")
            oe_profile_url = oe.get("metadata", {}).get("partner", {}).get("profile_url")
            oe_steam_name = oe.get("metadata", {}).get("partner", {}).get("steam_name")
            oe_tradeoffer_id = oe.get("tradeoffer_id")
            oe_status = oe.get('status')

            if oe_expires_soon and oe_status != TradeStatus.DISPUTED:
                if await self._already_purchased(oe_tradeoffer_id):
                    logger.info(f"[dispute] skip {oe_tradeoffer_id} — already recorded as "
                                f"purchased (mark-received likely failed earlier)")
                else:
                    await self.dispute_and_notify(
                        item_id=oe_item_id, market_name=oe_market_name,
                        created_at=oe_created_at, total_value=oe_total_value,
                        steam_name=oe_steam_name, steam_id64=oe_steamid64,
                        profile_url=oe_profile_url)

            for so in offers_steam[:]:
                so_tradeoffer_id = so.get("tradeofferid")
                so_steamid64 = self.usteamid_to_commid(so.get("accountid_other"))

                if not so.get("items_to_give"):
                    so_asset_id = int(list(so.get("items_to_receive", {}).keys())[0])
                    so_market_hash_name = next(
                        iter(so.get("items_to_receive", {}).values()), {}).get("market_hash_name")

                    if so_steamid64 == oe_steamid64 and so_market_hash_name == oe_market_name:
                        try:
                            accepted = await asyncio.to_thread(
                                self.steam.accept_trade_offer, so_tradeoffer_id)
                        except Exception as err:
                            logger.error(f"[accept] failed: {err}")
                            break

                        if accepted:
                            tasks.append(asyncio.create_task(self._mark_received_safe(
                                oe_item_id, so_tradeoffer_id, oe_tradeoffer_id, so_market_hash_name, oe_steam_name, so_steamid64)))
                            await self._record_purchase(
                                asset_id=so_asset_id, market_name=oe_market_name,
                                total_value=oe_total_value, seller_id=int(oe_steamid64),
                                seller_name=oe_steam_name, profile_url=oe_profile_url,
                                trade_id=oe_tradeoffer_id)
                            offers_steam.remove(so)

                        break
                else:
                    await asyncio.to_thread(self.steam.decline_trade_offer, so_tradeoffer_id)
                    logger.info(f"[decline] offer {so_tradeoffer_id} from {so_steamid64} "
                                f"— wants items from us")
                    offers_steam.remove(so)

        if offers_steam:
            logger.info(f"[offers] {len(offers_steam)} unmatched offer(s)")

        if tasks:
            await asyncio.gather(*tasks)

    # ------------------------------------------------------------------ #
    # per-trade actions
    # ------------------------------------------------------------------ #
    async def _already_purchased(self, trade_id) -> bool:
        """Guard before disputing: True if this trade is already in the DB. On a DB
        error assume not purchased so a genuinely expiring trade still gets disputed."""
        try:
            return await self.db.check_trade_id_exists(trade_id)
        except Exception as err:
            logger.error(f"[dispute] cannot verify trade {trade_id} before dispute: {err}")
            return False

    async def _record_purchase(self, asset_id, market_name, total_value, seller_id,
                               seller_name, profile_url, trade_id) -> None:
        """Persist the purchase. The Steam trade is already accepted at this point,
        so a DB failure means an unrecorded buy -> alert via Telegram to fix by hand."""
        try:
            await self.db.add_seller(steamid=seller_id, name=seller_name, profile_url=profile_url)
            await self.db.add_purchase_skin(
                asset_id=asset_id, market_hash_name=market_name,
                purchase_price_empire=total_value,
                purchase_price_float=self.from_empire_to_float_converter(float(total_value)),
                seller_id=seller_id, trade_id=trade_id,
                purchased_date=self.current_date_str())
        except Exception as db_err:
            logger.error(f"[db] write failed for accepted trade {trade_id}: {db_err}")
            try:
                await self.telegram.send_message(
                    f"⚠️ Trade accepted but NOT recorded in DB — add manually!\n\n"
                    f"{market_name},\n"
                    f"value: {total_value / 100},\n"
                    f"seller: {seller_name} ({seller_id}),\n"
                    f"trade_id: {trade_id},\n"
                    f"profile url: {profile_url}")
            except Exception as tg_err:
                logger.error(f"[db] failed to alert about unrecorded trade {trade_id}: {tg_err}")

    async def _mark_received_safe(self, item_id, tradeoffer_id, empire_offer_id, market_name, steam_name, steamid64 ) -> None:
        # NOTE: Empire's dispute/received endpoints are passed item_id here (not
        # tradeoffer_id as the docs label the path) — matches the working setup.
        try:
            await self.empire.mark_as_received(item_id)
            logger.info(f"[accept] {market_name} from {steam_name}, id64 {steamid64} (steam offer {tradeoffer_id}, empire offer {empire_offer_id}) marked as received")
        except CSGOEmpireError as err:
            if err.status == 404:
                logger.warning(f"[recv] steam offer {tradeoffer_id} not found (mark-as-received)")
            else:
                logger.error(f"[recv] mark-as-received failed: {err}")

    async def dispute_and_notify(self, item_id, market_name: str, created_at: str,
                                 total_value: int, steam_name: str, steam_id64: str,
                                 profile_url: str) -> None:
        try:
            await self.empire.dispute_trade(item_id)
        except CSGOEmpireError as err:
            logger.error(f"[dispute] failed on CSGOEmpire: {err}")
            return

        bought = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S") + timedelta(hours=1)
        message = (f"Withdraw disputed! \n\n"
                   f"{market_name},\n"
                   f"bought: {bought},\n"
                   f"value: {total_value / 100},\n"
                   f"nick: {steam_name}\n"
                   f"steamid: {steam_id64},\n"
                   f"profile url: {profile_url}")

        try:
            await self.telegram.send_message(message)
        except Exception as telegram_err:
            logger.warning(f"[telegram] not sent: {telegram_err}")
            await asyncio.sleep(3)
            try:
                await self.telegram.send_message(message)
            except Exception as retry_err:
                logger.error(f"[telegram] retry failed: {retry_err}")

        logger.info(f"[dispute] {market_name} — value {total_value / 100:.2f} C, "
                    f"bought {bought}, seller {steam_name} ({steam_id64})")
