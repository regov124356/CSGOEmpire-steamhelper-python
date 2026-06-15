"""
Async client for the CSGOEmpire trading API.

Implements the public API documented at
https://docs.csgoempire.com/reference/getting-started-with-your-api

Auth:        Authorization: Bearer <api_key>
Rate limit:  Global 120 requests / 60 seconds. Exceeding it returns HTTP 429
             and blocks every endpoint for 60 seconds. Individual endpoints
             carry their own (undocumented) limits, surfaced through the
             X-RateLimit-* / Retry-After response headers.

The client enforces the global limit proactively with a token bucket, adapts
to the per-endpoint limits via the rate-limit response headers, and retries
on 429 honouring Retry-After.
"""

import asyncio
import math
import time
from collections import deque
from enum import IntEnum
from typing import Any, Optional

import aiohttp

DEFAULT_HOST = "csgoempire.io"


class TradeStatus(IntEnum):
    """Numeric trade statuses returned by the trading endpoints."""
    ERROR = -1
    PENDING = 0
    RECEIVED = 1
    PROCESSING = 2
    SENDING = 3
    CONFIRMING = 4
    SENT = 5
    COMPLETED = 6
    DECLINED = 7
    CANCELED = 8
    TIMED_OUT = 9
    CREDITED = 10
    DISPUTED = 11
    COMPLETED_BUT_REVERSIBLE = 13
    TRADE_REVERSED = 14
    TIMED_OUT_BUT_REVERSIBLE = 15


class CSGOEmpireError(Exception):
    """Raised when a request fails after exhausting retries."""

    def __init__(self, message: str, status: Optional[int] = None,
                 payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class _RateLimiter:
    """
    Async token bucket over a rolling time window.

    Keeps at most ``max_requests`` acquisitions inside any ``window`` seconds.
    Acquisition is serialised so concurrent callers queue in FIFO order.
    """

    def __init__(self, max_requests: int, window: float):
        self.max_requests = max_requests
        self.window = window
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        # Hard pause until this monotonic time, set when the API reports it is
        # out of quota (X-RateLimit-Remaining: 0 or a 429 Retry-After).
        self._blocked_until = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()

                if now < self._blocked_until:
                    await asyncio.sleep(self._blocked_until - now)
                    continue

                while self._timestamps and now - self._timestamps[0] >= self.window:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return

                await asyncio.sleep(self.window - (now - self._timestamps[0]))

    def block_for(self, seconds: float) -> None:
        """Force every caller to wait at least ``seconds`` from now."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)


class CSGOEmpireClient:
    """
    Async wrapper around the CSGOEmpire trading API.

    Usage:
        async with CSGOEmpireClient(api_key) as client:
            trades = await client.get_active_trades()

    The client can also be used without the context manager; the underlying
    aiohttp session is created lazily and should then be closed with
    ``await client.close()``.
    """

    def __init__(self, api_key: str, *, host: str = DEFAULT_HOST,
                 max_requests: int = 120, window: float = 60.0,
                 max_retries: int = 3, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = f"https://{host}/api/v2"
        self.max_retries = max_retries
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._limiter = _RateLimiter(max_requests, window)
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------ #
    # session lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "CSGOEmpireClient":
        self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    # core request handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _retry_after(headers, default: float) -> float:
        value = headers.get("Retry-After")
        if value is not None:
            try:
                return float(value)
            except ValueError:
                pass
        return default

    def _note_rate_headers(self, headers) -> None:
        """If the API says we're out of quota, pause the limiter pre-emptively.

        X-RateLimit-Reset may be a seconds-to-wait duration or an absolute epoch
        depending on the backend; normalise to a duration and clamp so a bad/epoch
        value can never freeze the client.
        """
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            if int(remaining) > 0:
                return
            reset = float(reset)
        except ValueError:
            return
        now = time.time()
        wait = reset - now if reset > now else reset
        self._limiter.block_for(max(0.0, min(wait, 300.0)))

    @staticmethod
    def _clean_params(params: Optional[dict]) -> Optional[dict]:
        """
        Drop None values and coerce bools to the API's "yes"/"no" convention,
        since aiohttp/yarl only accept str/int/float query values.
        """
        if not params:
            return None
        cleaned: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                cleaned[key] = "yes" if value else "no"
            else:
                cleaned[key] = value
        return cleaned or None

    async def _request(self, method: str, path: str, *,
                       params: Optional[dict] = None,
                       json: Optional[Any] = None,
                       fail_fast_429: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        params = self._clean_params(params)
        session = self._ensure_session()
        attempt = 0

        while True:
            await self._limiter.acquire()
            try:
                async with session.request(method, url, params=params,
                                           json=json) as resp:
                    self._note_rate_headers(resp.headers)

                    if resp.status == 429:
                        wait = self._retry_after(resp.headers, 60.0)
                        self._limiter.block_for(wait)
                        # Time-sensitive callers (bids) raise immediately rather
                        # than block up to ~60s waiting out the rate limit.
                        if fail_fast_429 or attempt >= self.max_retries:
                            raise CSGOEmpireError(
                                f"Rate limited on {method} {path}",
                                status=429)
                        attempt += 1
                        await asyncio.sleep(wait)
                        continue

                    if resp.status >= 500:
                        if attempt >= self.max_retries:
                            text = await resp.text()
                            raise CSGOEmpireError(
                                f"Server error {resp.status} on {method} {path}",
                                status=resp.status, payload=text)
                        attempt += 1
                        await asyncio.sleep(2 ** attempt)
                        continue

                    data = await self._parse(resp)
                    if resp.status >= 400:
                        raise CSGOEmpireError(
                            f"HTTP {resp.status} on {method} {path}",
                            status=resp.status, payload=data)
                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                if attempt >= self.max_retries:
                    raise CSGOEmpireError(
                        f"Network error on {method} {path}: {err}") from err
                attempt += 1
                await asyncio.sleep(2 ** attempt)

    @staticmethod
    async def _parse(resp: aiohttp.ClientResponse) -> Any:
        try:
            return await resp.json()
        except (aiohttp.ContentTypeError, ValueError):
            return await resp.text()

    # ------------------------------------------------------------------ #
    # value helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def coins_to_coincents(coins: float) -> int:
        """1 coin = 100 coincents."""
        return int(round(coins * 100))

    @staticmethod
    def coincents_to_coins(coincents: int) -> float:
        return coincents / 100

    @staticmethod
    def min_next_bid(previous_bid: int) -> int:
        """
        Minimum valid next bid in coincents: exactly 1% above the previous bid,
        minimum increment of 1 coincent, rounded half away from zero.
        """
        increment = max(1, math.floor(previous_bid * 0.01 + 0.5))
        return previous_bid + increment

    # ------------------------------------------------------------------ #
    # metadata
    # ------------------------------------------------------------------ #
    async def get_metadata(self) -> Any:
        """GET /metadata/socket — account + socket auth metadata."""
        return await self._request("GET", "/metadata/socket")

    # ------------------------------------------------------------------ #
    # trading automation
    # ------------------------------------------------------------------ #
    async def get_automation_status(self) -> Any:
        """GET /trading/automation/status"""
        return await self._request("GET", "/trading/automation/status")

    async def update_access_token(self, access_token: str) -> Any:
        """PUT /trading/automation/access-token"""
        return await self._request(
            "PUT", "/trading/automation/access-token",
            json={"access_token": access_token})

    async def delete_access_token(self) -> Any:
        """DELETE /trading/automation/access-token"""
        return await self._request("DELETE", "/trading/automation/access-token")

    async def check_trades(self) -> Any:
        """POST /trading/automation/check-trades — manually trigger resolution."""
        return await self._request("POST", "/trading/automation/check-trades")

    # ------------------------------------------------------------------ #
    # trades / auctions
    # ------------------------------------------------------------------ #
    async def get_active_trades(self) -> Any:
        """GET /trading/user/trades — active deposits and withdrawals."""
        return await self._request("GET", "/trading/user/trades")

    async def get_trade(self, item_id: int, trade_type: str) -> Any:
        """
        GET /trading/user/trade/{item_id}/{trade_type}

        item_id: deposit ID, trade ID or bid ID.
        trade_type: one of "bid", "deposit", "withdrawal".
        """
        return await self._request(
            "GET", f"/trading/user/trade/{item_id}/{trade_type}")

    async def get_active_auctions(self) -> Any:
        """GET /trading/user/auctions"""
        return await self._request("GET", "/trading/user/auctions")

    # ------------------------------------------------------------------ #
    # deposits
    # ------------------------------------------------------------------ #
    async def create_deposit(self, items: list[dict]) -> Any:
        """
        POST /trading/deposit — list items for sale.

        items: list of {"id" | "asset_id": int, "coin_value": int}.
        Max 20 items per request is recommended.
        """
        return await self._request("POST", "/trading/deposit",
                                   json={"items": items})

    async def cancel_deposit(self, deposit_id: int) -> Any:
        """POST /trading/deposit/{deposit_id}/cancel"""
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/cancel")

    async def cancel_deposits(self, ids: list[int]) -> Any:
        """POST /trading/deposit/cancel — cancel many deposits at once."""
        return await self._request("POST", "/trading/deposit/cancel",
                                   json={"ids": ids})

    async def get_deposit_status(self, tracking_code: str) -> Any:
        """GET /trading/deposit/status/{tracking_code}"""
        return await self._request(
            "GET", f"/trading/deposit/status/{tracking_code}")

    async def sell_now(self, deposit_id: int) -> Any:
        """POST /trading/deposit/{deposit_id}/sell — sell to the highest bid."""
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/sell")

    async def mark_as_sent(self, deposit_id: int) -> Any:
        """POST /trading/deposit/{deposit_id}/sent"""
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/sent")

    async def mark_as_received(self, tradeoffer_id: int) -> Any:
        """POST /trading/deposit/{tradeoffer_id}/received"""
        return await self._request(
            "POST", f"/trading/deposit/{tradeoffer_id}/received")

    async def dispute_trade(self, tradeoffer_id: int) -> Any:
        """POST /trading/deposit/{tradeoffer_id}/dispute"""
        return await self._request(
            "POST", f"/trading/deposit/{tradeoffer_id}/dispute")

    async def create_withdrawal(self, deposit_id: int, coin_value: int) -> Any:
        """POST /trading/deposit/{deposit_id}/withdraw — buy/withdraw an item."""
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/withdraw",
            json={"coin_value": coin_value})

    async def place_bid(self, deposit_id: int, bid_value: int, *,
                        fail_fast_429: bool = False) -> Any:
        """
        POST /trading/deposit/{deposit_id}/bid

        bid_value in coincents; must be at least 1% above the current bid
        (see ``min_next_bid``).

        fail_fast_429: raise immediately on 429 instead of waiting out the rate
        limit — auctions are time-sensitive, so a 60s wait wins nothing.
        """
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/bid",
            json={"bid_value": bid_value}, fail_fast_429=fail_fast_429)

    async def get_depositor_stats(self, deposit_id: int) -> Any:
        """GET /trading/deposit/{deposit_id}/stats"""
        return await self._request(
            "GET", f"/trading/deposit/{deposit_id}/stats")

    # ------------------------------------------------------------------ #
    # listings (price management)
    # ------------------------------------------------------------------ #
    async def update_listing_price(self, deposit_or_asset_id: int,
                                   coin_value: int) -> Any:
        """PATCH /trading/deposit/{deposit_or_asset_id}"""
        return await self._request(
            "PATCH", f"/trading/deposit/{deposit_or_asset_id}",
            json={"coin_value": coin_value})

    async def bulk_update_listing_prices(self, items: list[dict]) -> Any:
        """
        PATCH /trading/deposit/bulk

        items: list of {"id": int, "coin_value": int}. Max 20 per request.
        """
        return await self._request("PATCH", "/trading/deposit/bulk",
                                   json={"items": items})

    async def get_listed_items(self, *, per_page: int, page: int,
                               **filters: Any) -> Any:
        """
        GET /trading/items — browse the marketplace.

        Required: per_page, page. Optional filters (auction, sort, search,
        order, price_min, price_max, price_max_above, wear_min, wear_max,
        has_stickers, is_commodity, delivery_time_long_min/max, ...) are
        passed through as query parameters.
        """
        params = {"per_page": per_page, "page": page, **filters}
        return await self._request("GET", "/trading/items", params=params)

    # ------------------------------------------------------------------ #
    # inventory
    # ------------------------------------------------------------------ #
    async def get_inventory(self, *, invalid: str = "yes") -> Any:
        """
        GET /trading/user/inventory

        invalid: "yes" (default) filters out invalid items.
        """
        return await self._request("GET", "/trading/user/inventory",
                                   json={"invalid": invalid})

    # ------------------------------------------------------------------ #
    # block list
    # ------------------------------------------------------------------ #
    async def get_blocked_users(self) -> Any:
        """GET /trading/block-list"""
        return await self._request("GET", "/trading/block-list")

    async def block_user(self, steam_id: str) -> Any:
        """POST /trading/block-list/{steam_id}"""
        return await self._request("POST", f"/trading/block-list/{steam_id}")

    async def unblock_user(self, steam_id: str) -> Any:
        """DELETE /trading/block-list/{steam_id}"""
        return await self._request("DELETE", f"/trading/block-list/{steam_id}")

    # ------------------------------------------------------------------ #
    # account / user
    # ------------------------------------------------------------------ #
    async def update_settings(self, *, trade_url: str,
                              marketplace_privacy_protection_level: str = "base"
                              ) -> Any:
        """POST /trading/user/settings"""
        return await self._request(
            "POST", "/trading/user/settings",
            json={
                "trade_url": trade_url,
                "marketplace_privacy_protection_level":
                    marketplace_privacy_protection_level,
            })

    async def get_transactions(self, page: Optional[int] = None) -> Any:
        """GET /user/transactions"""
        params = {"page": page} if page is not None else None
        return await self._request("GET", "/user/transactions", params=params)

    async def tip(self, amount: int, *, user_id: Optional[str] = None,
                  steam_id: Optional[str] = None) -> Any:
        """
        POST /user/tip — requires 2FA on the account.

        amount in coincents. Provide exactly one of user_id or steam_id.
        """
        if (user_id is None) == (steam_id is None):
            raise ValueError("Provide exactly one of user_id or steam_id")
        body: dict[str, Any] = {"amount": str(amount)}
        if user_id is not None:
            body["user_id"] = user_id
        else:
            body["steam_id"] = steam_id
        return await self._request("POST", "/user/tip", json=body)
