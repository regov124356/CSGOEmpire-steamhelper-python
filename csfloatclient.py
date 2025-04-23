import math
from collections import defaultdict

from csfloat_api.csfloat_client import Client, Me


class CSFloatClient(Client):
    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.divider: float = None
        self._inventory = []


    def get_divider(self):
        return self.divider

    def set_divider(self, divider: float):
        self.divider = divider

    async def get_steamid64(self):
        me = await self.get_me()
        return me.user.steam_id

    async def check_price(self, market_hash_name: str) -> tuple[int, int]:
        if "&" in market_hash_name:
            market_hash_name = market_hash_name.replace("&", "%26")

        listings = await self.get_all_listings(sort_by="lowest_price", type_="buy_now",
                                               market_hash_name=market_hash_name)

        if not listings:
            return 0, 0

        first_item = listings[0]
        listing_id = first_item.id
        price = first_item.price

        buy_orders = await self.get_buy_orders(listing_id=listing_id)

        if not buy_orders:
            return 0, 0

        buy_orders_list = []
        for bo in buy_orders:
            if not bo.expression:
                buy_orders_list.append(bo)

        if not buy_orders_list:
            return 0, 0

        first_buy_order = buy_orders_list[0]
        buy_order_price = first_buy_order.price

        float_price = math.floor(buy_order_price if price < 100 else (price + buy_order_price) / 2)

        after_fee = math.floor(float_price * 0.98)
        empire_price = math.floor(after_fee / self.divider)
        #print(f'{market_hash_name}: {empire_price / 100}')

        return empire_price, float_price

    async def get_filtered_pending_trades(self) -> dict[str, list[str]]:
        results = await self.get_pending_trades()
        trades = results['trades']

        pending_items = defaultdict(list)

        for trade in trades:
            item = trade['contract']['item']
            if item['is_commodity']:
                pending_items[item['item_name']].append(item['asset_id'])

        return pending_items

    async def get_my_stall(self, limit: int = 1000):
        steamid64 = await self.get_steamid64()
        parameters = f"/users/{steamid64}/stall?limit={limit}"
        method = "GET"

        response = await self._request(key="get_me", method=method, parameters=parameters)
        return response