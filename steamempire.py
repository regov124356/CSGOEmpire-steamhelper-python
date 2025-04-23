import asyncio
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from time import sleep

import requests
from steampy.client import SteamClient
from steampy.exceptions import InvalidCredentials
from steampy.models import GameOptions

from telegram import Telegram
from db import DB

DOMAIN = 'csgoempire.io'

class SteamEmpire(SteamClient):
    def __init__(self, api_key: str, username: str, password: str, steam_guard: str, bearer_auth: str,
                 telegram: Telegram, db: DB, divider: float):
        super().__init__(api_key=api_key)
        print("Logging in to steam...")
        try:
            super().login(username=username, password=password, steam_guard=steam_guard)
            print("Logged in.")
        except InvalidCredentials or ValueError as err:
            print(f"Error: {err}")
            sys.exit()

        self.game = GameOptions.CS
        self.bearer_auth = bearer_auth
        self.current_offers = []
        self.telegram = telegram
        self.db = db
        self.divider = divider

        self.csgoempire_headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.bearer_auth}"
        }


    @staticmethod
    def usteamid_to_commid(accountid) -> str:
        """
        Convert usteamid to steamid64
        """
        return str(76561197960265728 + accountid)

    async def get_automation_status(self):
        url = f'https://{DOMAIN}/api/v2/trading/automation/status'

        try:
            response = requests.get(url=url, headers=self.csgoempire_headers).json()
            return response
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now()}] - Error getting automation status from CSGOEmpire: {e}")
            return {}

    async def put_automation_access_token(self):
        url = f'https://{DOMAIN}/api/v2/trading/automation/access-token'
        body = {"access_token" : self.get_access_token()}
        try:
            response = requests.put(url=url, headers=self.csgoempire_headers, data=body).json()
            return response
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now()}] - Error putting automation access_token to CSGOEmpire: {e}")

    async def update_automation(self):
        while True:
            automation_status = await self.get_automation_status()
            if automation_status.get('success'):
                data = automation_status.get('data', {})

                if not data.get('has_access_token', False):
                    result = await self.put_automation_access_token()
                    if not result.get('success'):
                        print(result)
                else:
                    access_token_expires_at = data.get('access_token_expires_at')
                    if access_token_expires_at:
                        expiry_time = datetime.fromisoformat(access_token_expires_at.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        wait_time = (expiry_time - now).total_seconds()

                        if wait_time > 0:
                            await asyncio.sleep(wait_time + 1)

    def get_active_trades_on_empire(self) -> list:
        url = f'https://{DOMAIN}/api/v2/trading/user/trades'

        try:
            response = requests.get(url=url, headers=self.csgoempire_headers).json()

            offers_on_empire = []
            withdrawals = response.get("data", {}).get("withdrawals", [])
            for offer in withdrawals:
                if offer.get("status") == 5 or offer.get("status") == 11:
                    offers_on_empire.append(offer)
            self.current_offers = offers_on_empire
            return offers_on_empire
        except requests.exceptions.RequestException as e:
            print(f"[{datetime.now()}] - Error fetching trades from CSGOEmpire: {e}")
            return []

    @staticmethod
    def current_date_str():
        return str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    def from_empire_to_float_converter(self, price_empire: float) -> float:
        return round((price_empire * self.divider) / 0.98, 2)

    async def dispute_trade_on_empire(self, deposit_id, market_name: str, created_at: str, total_value: int,
                                      steam_name: str,
                                      steam_id64: str,
                                      profile_url: str) -> None:
        url = f'https://{DOMAIN}/api/v2/trading/deposit/{deposit_id}/dispute'

        try:
            response = requests.post(url=url, headers=self.csgoempire_headers)
            response.raise_for_status()

            date_obj = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
            new_date_obj = date_obj + timedelta(hours=1)

            try:
                await self.telegram.send_message(f"Withdraw disputed! \n\n"
                                                 f"{market_name},\n"
                                                 f"bought: {new_date_obj},\n"
                                                 f"value: {total_value / 100},\n"
                                                 f"nick: {steam_name}\n"
                                                 f"steamid: {steam_id64},\n"
                                                 f"profile url: {profile_url}")
            except Exception as telegram_err:
                print(f"[{datetime.now()}] - Telegram was not sent: {telegram_err}")
                time.sleep(3)
                await self.telegram.send_message(f"Withdraw disputed! \n\n"
                                                 f"{market_name},\n"
                                                 f"bought: {new_date_obj},\n"
                                                 f"value: {total_value / 100},\n"
                                                 f"nick: {steam_name}\n"
                                                 f"steamid: {steam_id64},\n"
                                                 f"profile url: {profile_url}")

            print(
                f"[{datetime.now()}] - {market_name} disputed successfully, bought: {new_date_obj}, value: {total_value / 100}, steam_name: {steam_name}, steamid: {steam_id64}, profile url: {profile_url}")

        except requests.exceptions.HTTPError or requests.exceptions.RequestException as err:
            print(f"[{datetime.now()}] - Error disputing trade on CSGOEmpire: {err}")
            time.sleep(3)
            await self.dispute_trade_on_empire(deposit_id, market_name, created_at, total_value, steam_name, steam_id64,
                                               profile_url)

    async def mark_as_sent_on_empire(self, deposit_id) -> None:
        url = f'https://{DOMAIN}/api/v2/trading/deposit/{deposit_id}/sent'

        try:
            response = requests.post(url=url, headers=self.csgoempire_headers)
            response.raise_for_status()

        except requests.exceptions.HTTPError or requests.exceptions.RequestException as err:
            print(f"[{datetime.now()}] - Error marking as sent trade on CSGOEmpire: {err}")
            time.sleep(3)
            await self.mark_as_sent_on_empire(deposit_id)

    async def mark_as_received_on_empire(self, deposit_id, tradeoffer_id, os_market_name: str, os_steamid64) -> None:
        url = f'https://{DOMAIN}/api/v2/trading/deposit/{deposit_id}/received'

        try:
            response = requests.post(url=url, headers=self.csgoempire_headers)
            response.raise_for_status()

            print(f"[{datetime.now()}] - Offer {tradeoffer_id}, {os_market_name} from {os_steamid64} accepted.")

        except requests.exceptions.HTTPError or requests.exceptions.RequestException as err:
            if err.response.status_code == 404:
                print(f"[{datetime.now()}] - Offer {tradeoffer_id} not found, marking as received trade error.")
                return

            print(f"[{datetime.now()}] - Error marking as received trade on CSGOEmpire: {err}")
            time.sleep(3)
            await self.mark_as_received_on_empire(deposit_id, tradeoffer_id, os_market_name, os_steamid64)

    async def accept_matching_offers(self, offers_empire: list, offers_steam: dict) -> None:
        offers_steam = offers_steam.get("response", {}).get("trade_offers_received", [])
        tasks = []

        for oe in offers_empire[:]:
            oe_steamid64 = oe.get("metadata", {}).get("partner", {}).get("steam_id")
            oe_market_name = oe.get("item", {}).get("market_name")
            oe_item_id = oe.get("item_id")
            oe_expires_at = int(oe.get("metadata", {}).get("expires_at")) - int(time.time()) < 540
            oe_created_at = oe.get("created_at")
            oe_total_value = oe.get("total_value")
            oe_profile_url = oe.get("metadata", {}).get("partner", {}).get("profile_url")
            oe_steam_name = oe.get("metadata", {}).get("partner", {}).get("steam_name")
            oe_tradeoffer_id = oe.get("tradeoffer_id")
            oe_status = oe.get('status')

            if oe_expires_at and oe_status != 11:
                            await self.dispute_trade_on_empire(deposit_id=oe_item_id, market_name=oe_market_name,
                                                                created_at=oe_created_at, total_value=oe_total_value,
                                                                steam_name=oe_steam_name, steam_id64=oe_steamid64,
                                                                profile_url=oe_profile_url)

            for os in offers_steam[:]:
                os_tradeoffer_id = os.get("tradeofferid")
                os_steamid64 = self.usteamid_to_commid(os.get("accountid_other"))

                if not os.get("items_to_give"):
                    os_asset_id = int(list(os.get("items_to_receive", {}).keys())[0])
                    os_market_hash_name = next(iter(os.get("items_to_receive", {}).values()), {}).get(
                        "market_hash_name")

                    if os_steamid64 == oe_steamid64 and os_market_hash_name == oe_market_name:
                        try:
                            if self.accept_trade_offer(os_tradeoffer_id):
                                tasks.append(asyncio.create_task(
                                    self.mark_as_received_on_empire(oe_item_id, os_tradeoffer_id, os_market_hash_name,
                                                                    os_steamid64)))
                                await self.db.add_seller(steamid=int(oe_steamid64), name=oe_steam_name,
                                                        profile_url=oe_profile_url)
                                tasks.append(asyncio.create_task(
                                    self.db.add_purchase_skin(asset_id=os_asset_id, market_hash_name=oe_market_name,
                                                            purchase_price_empire=oe_total_value, purchase_price_float=self.from_empire_to_float_converter(float(oe_total_value)), seller_id=int(oe_steamid64),
                                                            trade_id=oe_tradeoffer_id, purchased_date=self.current_date_str())))
                                
                            offers_steam.remove(os)
                            
                        except Exception as err:
                            print(f"{err}: Error in accepting trade offer")
                        
                        break
                else:
                    self.decline_trade_offer(os_tradeoffer_id)
                    print(
                        f"[{datetime.now()}] - Offer {os_tradeoffer_id} from {os_steamid64} declined (items to give).")
                    offers_steam.remove(os)
                


        if len(offers_steam):
            print(f"[{datetime.now()}] - {len(offers_steam)} offers do not match to any withdrawals.")

        if tasks:
            await asyncio.gather(*tasks)

    def get_tradable_items(self, commodity: int = 1) -> dict[str, list[str]]:
        items = defaultdict(list)
        inventory = self.get_my_inventory(self.game)

        for item in inventory.values():
            if item['tradable'] == 1:
                if commodity == 2 or item['commodity'] == commodity:
                    item_name = item['market_name']
                    asset_id = item['id']

                    items[item_name].append(asset_id)
        return items