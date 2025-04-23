import asyncio

import pyodbc
from mysql.connector import Error


class DB:
    def __init__(self, user: str, password: str, host: str, database: str):
        self._config = 'DRIVER={ODBC Driver 17 for SQL Server};'\
                       f'SERVER={host};'\
                       f'DATABASE={database};'\
                       f'UID={user}; '\
                       f'PWD={password}'
        self._connection = None
        self._cursor = None
        self.connection_connect()

    def connection_connect(self):
        try:
            self._connection = pyodbc.connect(self._config)
            self._cursor = self._connection.cursor()
        except Error as err:
            print(f"Error connecting to database: {err}")
            raise

    def connection_close(self):
        try:
            if self._cursor:
                self._cursor.close()
            if self._connection:
                self._connection.close()
        except Error as err:
            print(f"Error closing connection: {err}")

    def check_item_prices(self) -> list[tuple]:
        select_query = f"SELECT id, market_hash_name FROM items JOIN (SELECT item_id FROM item_prices) as tab ON items.id = tab.item_id;"
        try:
            self._cursor.execute(select_query)
            return self._cursor.fetchall()

        except Error as err:
            print(f"Error fetching rows: {err}")
            return []

    def check_sellers(self, steamid: int) -> list[tuple]:
        select_query = f"SELECT steamid, name FROM sellers WHERE steamid=?"
        try:
            self._cursor.execute(select_query, (steamid,))
            return self._cursor.fetchall()

        except Error as err:
            print(f"Error fetching rows: {err}")
            return []


    async def update_items_prices(self, id: int, price_empire: float, price_float: float) -> None:
        update_query = "UPDATE item_prices SET price_empire=?, price_float=? WHERE item_id=?"
        try:
            self._cursor.execute(update_query, (price_empire, price_float, id))
            self._connection.commit()
        except Error as err:
            print(f"Error updating rows: {err}")
            await asyncio.sleep(3)
            await self.update_items_prices(id, price_empire, price_float)



    async def add_seller(self, steamid: int, name: str, profile_url: str):
        query = "EXEC AddSeller @steamid=?, @name=?, @profile_url=?"
        try:
            self._cursor.execute(query, (steamid, name, profile_url))

            self._connection.commit()

        except Error as err:
            print(f"Error updating rows: {err}")
            await asyncio.sleep(3)
            await self.add_seller(steamid, name, profile_url)


    async def add_purchase_skin(self, asset_id: int, market_hash_name: str, purchase_price_empire: float, purchase_price_float: float,  seller_id: int, trade_id: int, purchased_date: str):
        query = "EXEC AddPurchasedSkin @asset_id=?, @market_hash_name=?, @purchase_price_empire=?, @purchase_price_float=?, @seller_id=?, @trade_id=?, @purchased_date=?"
        try:
            self._cursor.execute(query, (asset_id, market_hash_name, purchase_price_empire, purchase_price_float, seller_id, trade_id, purchased_date))
            result = self._cursor.fetchone()
            self._connection.commit()
            return result
        except Error as err:
            print(f"Error updating rows: {err}")
            await asyncio.sleep(3)
            await self.add_purchase_skin(asset_id, market_hash_name, purchase_price_empire, purchase_price_float, seller_id, trade_id,
                                         purchased_date)