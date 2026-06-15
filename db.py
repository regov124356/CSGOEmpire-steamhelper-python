from logger import logger
import asyncio

import pyodbc

# SQLSTATEs that mean "the connection dropped" -> reconnect and retry.
_CONNECTION_SQLSTATES = {'08S01', '08003', '08007', '08S02'}


class DBError(Exception):
    """Raised when a query keeps failing after exhausting retries."""


class DB:
    def __init__(self, user: str, password: str, host: str, database: str,
                 *, retries: int = 3, retry_delay: float = 3.0):
        self._config = (
            'DRIVER={ODBC Driver 17 for SQL Server};'
            f'SERVER={host};DATABASE={database};UID={user};PWD={password}'
        )
        self._retries = retries
        self._retry_delay = retry_delay
        self._connection = None
        # Serialises access to the single pyodbc connection (not thread-safe for
        # concurrent cursors). Created here; safe outside a running loop in 3.10+.
        self._lock = asyncio.Lock()
        self.connection_connect()

    # ------------------------------------------------------------------ #
    # connection management
    # ------------------------------------------------------------------ #
    def connection_connect(self) -> None:
        try:
            self._connection = pyodbc.connect(self._config, autocommit=False)
            logger.info("Connected to the database.")
        except pyodbc.Error as err:
            logger.exception(f"Error in connection_connect: {err}")
            raise

    def connection_close(self) -> None:
        try:
            if self._connection:
                self._connection.close()
        except pyodbc.Error as err:
            logger.exception(f"Error in connection_close: {err}")

    def connection_reconnect(self) -> None:
        self.connection_close()
        self.connection_connect()

    def is_connected(self) -> bool:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT 1;")
            return True
        except pyodbc.Error:
            return False

    @staticmethod
    def is_connection_error(err: pyodbc.Error) -> bool:
        sqlstate = getattr(err, 'sqlstate', None)
        if sqlstate in _CONNECTION_SQLSTATES:
            return True
        if sqlstate == 'HY000' and 'connection' in str(err).lower():
            return True
        return 'connection' in str(err).lower()

    def _safe_reconnect(self) -> None:
        try:
            self.connection_reconnect()
        except pyodbc.Error as err:
            logger.exception(f"Reconnect failed: {err}")

    # ------------------------------------------------------------------ #
    # generic execution: one place for locking, threading and retries
    # ------------------------------------------------------------------ #
    async def _execute(self, query: str, params: tuple = (), *,
                       fetch: str | None = None, commit: bool = False):
        """Run a query off the event loop, retrying on transient DB errors.

        fetch: None | "one" | "all". Returns the fetched rows (or None).
        Raises DBError if every attempt fails.
        """
        async with self._lock:
            last_err = None
            for _ in range(self._retries):
                try:
                    return await asyncio.to_thread(self._execute_sync, query, params, fetch, commit)
                except pyodbc.Error as err:
                    last_err = err
                    logger.exception(
                        f"DB error [{getattr(err, 'sqlstate', None)}] on query: {query}")
                    if self.is_connection_error(err):
                        await asyncio.to_thread(self._safe_reconnect)
                    await asyncio.sleep(self._retry_delay)
            raise DBError(f"Query failed after {self._retries} attempts: {query}") from last_err

    def _execute_sync(self, query: str, params: tuple, fetch: str | None, commit: bool):
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(query, params)
                result = None
                if fetch == "all":
                    result = cursor.fetchall()
                elif fetch == "one":
                    result = cursor.fetchone()
                if commit:
                    self._connection.commit()
                return result
        except pyodbc.Error:
            try:
                self._connection.rollback()
            except pyodbc.Error:
                pass
            raise

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    async def check_item_prices(self) -> list[tuple]:
        query = (
            "SELECT i.id, i.market_hash_name "
            "FROM items i JOIN item_prices ip ON i.id = ip.item_id"
        )
        return await self._execute(query, fetch="all")

    async def get_item_price(self, market_hash_name: str) -> tuple | None:
        """Our max Empire price and when it was last refreshed, as
        ``(price_empire, price_updated_at)``, or None if the item isn't
        tracked/priced. ``price_updated_at`` is naive UTC (from SYSUTCDATETIME)
        and may be NULL for a never-refreshed row; the caller decides freshness."""
        query = (
            "SELECT ip.price_empire, ip.price_updated_at FROM item_prices ip "
            "JOIN items i ON ip.item_id = i.id "
            "WHERE i.market_hash_name=?"
        )
        row = await self._execute(query, (market_hash_name,), fetch="one")
        return tuple(row) if row else None

    async def update_items_prices(self, id: int, price_empire: float,
                                  price_float: float) -> None:
        query = (
            "UPDATE item_prices "
            "SET price_empire=?, price_float=?, price_updated_at=SYSUTCDATETIME() "
            "WHERE item_id=?"
        )
        await self._execute(query, (price_empire, price_float, id), commit=True)

    async def add_seller(self, steamid: int, name: str, profile_url: str) -> None:
        query = "EXEC AddSeller @steamid=?, @name=?, @profile_url=?"
        await self._execute(query, (steamid, name, profile_url), commit=True)

    async def add_purchase_skin(self, asset_id: int, market_hash_name: str,
                                purchase_price_empire: float, purchase_price_float: float,
                                seller_id: int, trade_id: int, purchased_date: str) -> None:
        query = (
            "EXEC AddPurchasedSkin @asset_id=?, @market_hash_name=?, @purchase_price_empire=?, "
            "@purchase_price_float=?, @seller_id=?, @trade_id=?, @purchased_date=?"
        )
        await self._execute(
            query,
            (asset_id, market_hash_name, purchase_price_empire, purchase_price_float,
             seller_id, trade_id, purchased_date),
            commit=True)

    async def check_trade_id_exists(self, trade_id: int) -> bool:
        query = "SELECT 1 FROM purchased_skins WHERE trade_id=?"
        row = await self._execute(query, (trade_id,), fetch="one")
        return row is not None
