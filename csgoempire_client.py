"""
Async client for the CSGOEmpire trading API.

Implements the public API documented at
https://docs.csgoempire.com/reference/getting-started-with-your-api

Auth:        Authorization: Bearer <api_key>
Rate limit:  Global request limit per key; exceeding any limit returns HTTP 429
             and blocks every endpoint for 60 seconds. The two official sources
             disagree on the global window: the reference page says 120 / 60s,
             the API-Docs README says 120 / 10s. We use the safer 120 / 60s — if
             the real window is shorter we are merely more conservative; the
             reverse would 429. Several endpoints carry tighter documented limits
             (e.g. Place Bid 20/10s, Get Active Trades 3/10s) — see ENDPOINT_LIMITS.

The client enforces the global limit and the per-endpoint limits proactively
with token buckets (so it self-throttles before Empire 429s), prioritises money
actions over bids over polling, and retries on 429 honouring Retry-After.
"""

import asyncio
import heapq
import math
import re
import time
from collections import Counter, deque
from enum import IntEnum
from typing import Any, Optional

import aiohttp

from logger import logger

DEFAULT_HOST = "csgoempire.io"

# Rate-limit priority tiers (lower = served first when the window is saturated).
# HIGH: TradeBot money actions (accept/dispute/withdraw real items) — must never
# wait behind a bid burst. BID: time-sensitive auction bids — outrank polling.
# NORMAL: background polling / metadata — yields to both.
PRIORITY_HIGH = 0
PRIORITY_BID = 1
PRIORITY_NORMAL = 2

# Per-endpoint limits Empire enforces on top of the global limit (any 429 blocks
# ALL endpoints for 60s, so we throttle preventively to avoid them).
# Keys are normalised "METHOD /path" with numeric ids collapsed to <id>.
# Values are (max_requests, window_seconds) set just under Empire's documented
# caps for headroom. Docs: github.com/OfficialCSGOEmpire/API-Docs.
ENDPOINT_LIMITS: dict[str, tuple[int, float]] = {
    "POST /trading/deposit/<id>/bid": (18, 10.0),  # doc 20/10 (success+fail)
    "GET /trading/user/trades":       (2, 10.0),   # doc 3/10
}
# Only endpoints we actually call AND that Empire caps below the global limit get
# a bucket. Omitted because currently unused (add back if wired up, using their
# documented caps): create_deposit (POST /trading/deposit, 20/10),
# create_withdrawal (POST /trading/deposit/<id>/withdraw, 8/10 success, 2/10
# failure). Everything else we call (auctions, metadata, automation status/token,
# mark-received, dispute) is documented as global-only, so the global limiter
# covers it.

# Endpoints not listed fall back to the global limiter only (e.g. get_metadata,
# get_active_auctions, mark_as_received, dispute_trade — documented as global).


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
    Async token bucket over a rolling time window, with priority.

    Keeps at most ``max_requests`` acquisitions inside any ``window`` seconds.
    Acquirers pass a priority (lower = served first); when the window is
    saturated the lowest-priority-number waiter takes the next free slot, so a
    money action outranks a bid and a bid outranks background polling. A single
    dispatcher task hands out the slots, so slot ordering is well defined and
    there is no race on the timestamp window even when many coroutines (TradeBot
    + BiddingBot) share one limiter.
    """

    def __init__(self, max_requests: int, window: float):
        self.max_requests = max_requests
        self.window = window
        self._timestamps: deque[float] = deque()
        # Hard pause until this monotonic time, set when the API reports it is
        # out of quota (X-RateLimit-Remaining: 0 or a 429 Retry-After).
        self._blocked_until = 0.0
        # Min-heap of (priority, seq, future); lower priority served first.
        # seq breaks ties FIFO within a priority class.
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = 0
        self._dispatcher: Optional[asyncio.Task] = None

    def _drain_expired(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window:
            self._timestamps.popleft()

    def _delay_until_slot(self, now: float) -> float:
        """Seconds until a slot is free, or 0.0 if one is free right now."""
        if now < self._blocked_until:
            return self._blocked_until - now
        self._drain_expired(now)
        if len(self._timestamps) < self.max_requests:
            return 0.0
        return self.window - (now - self._timestamps[0])

    async def acquire(self, priority: int = PRIORITY_NORMAL) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, self._seq, fut))
        self._seq += 1
        self._ensure_dispatcher()
        await fut

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None or self._dispatcher.done():
            self._dispatcher = asyncio.ensure_future(self._dispatch())

    async def _dispatch(self) -> None:
        # One unexpected error must not silently kill the dispatcher and strand
        # every queued waiter, so each iteration is guarded; the loop keeps going.
        while self._waiters:
            try:
                delay = self._delay_until_slot(time.monotonic())
                if delay > 0.0:
                    await asyncio.sleep(delay)
                    continue
                _, _, fut = heapq.heappop(self._waiters)
                if fut.done():           # caller cancelled while queued
                    continue
                self._timestamps.append(time.monotonic())
                fut.set_result(None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[ratelimit] dispatcher iteration error — continuing")

    def block_for(self, seconds: float) -> None:
        """Force every caller to wait at least ``seconds`` from now."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)
        self._ensure_dispatcher()


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
        # Tighter buckets for the endpoints Empire rate-limits below the global
        # cap; a request acquires its endpoint bucket (if any) and the global one.
        self._endpoint_limiters: dict[str, _RateLimiter] = {
            key: _RateLimiter(m, w) for key, (m, w) in ENDPOINT_LIMITS.items()
        }
        self._session: Optional[aiohttp.ClientSession] = None
        # TEMP(429-bug): rolling log of outgoing requests to count the burst that
        # trips a 429. Remove once the rate-limit cause is found.
        self._dbg_calls: deque[tuple[float, str]] = deque()

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

    @staticmethod
    def _endpoint_key(method: str, path: str) -> str:
        """Normalise to "METHOD /path" with numeric ids collapsed to <id>, so
        /deposit/123/bid and /deposit/456/bid map to the same ENDPOINT_LIMITS key."""
        return f"{method} {re.sub(r'/\d+', '/<id>', path)}"

    # TEMP(429-bug): request-rate instrumentation. Remove with self._dbg_calls,
    # the _dbg_record() call in _request, and the _dbg_report_429() call in the
    # 429 branch once the burst that trips the rate limit is identified.
    def _dbg_endpoint(self, method: str, path: str) -> str:
        return self._endpoint_key(method, path)

    def _dbg_record(self, method: str, path: str) -> None:
        now = time.monotonic()
        self._dbg_calls.append((now, self._dbg_endpoint(method, path)))
        cutoff = now - self._limiter.window
        while self._dbg_calls and self._dbg_calls[0][0] < cutoff:
            self._dbg_calls.popleft()

    def _dbg_report_429(self, method: str, path: str) -> None:
        now = time.monotonic()
        cutoff = now - self._limiter.window
        recent = [ep for ts, ep in self._dbg_calls if ts >= cutoff]
        by_ep = Counter(recent).most_common()
        breakdown = ", ".join(f"{ep}={n}" for ep, n in by_ep)
        logger.warning(
            f"[ratelimit-debug] 429 on {method} {path} — "
            f"{len(recent)} requests this process in trailing "
            f"{self._limiter.window:.0f}s | {breakdown}")

    async def _request(self, method: str, path: str, *,
                       params: Optional[dict] = None,
                       json: Optional[Any] = None,
                       fail_fast_429: bool = False,
                       priority: int = PRIORITY_NORMAL) -> Any:
        url = f"{self.base_url}{path}"
        params = self._clean_params(params)
        session = self._ensure_session()
        endpoint_limiter = self._endpoint_limiters.get(self._endpoint_key(method, path))
        attempt = 0

        while True:
            # Acquire the tighter endpoint bucket first, then the global one, so a
            # request only takes a global slot once its endpoint slot is free.
            if endpoint_limiter is not None:
                await endpoint_limiter.acquire(priority)
            await self._limiter.acquire(priority)
            self._dbg_record(method, path)  # TEMP(429-bug)
            try:
                async with session.request(method, url, params=params,
                                           json=json) as resp:
                    self._note_rate_headers(resp.headers)

                    if resp.status == 429:
                        self._dbg_report_429(method, path)  # TEMP(429-bug)
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
            "POST", f"/trading/deposit/{tradeoffer_id}/received",
            priority=PRIORITY_HIGH)

    async def dispute_trade(self, tradeoffer_id: int) -> Any:
        """POST /trading/deposit/{tradeoffer_id}/dispute"""
        return await self._request(
            "POST", f"/trading/deposit/{tradeoffer_id}/dispute",
            priority=PRIORITY_HIGH)

    async def create_withdrawal(self, deposit_id: int, coin_value: int) -> Any:
        """POST /trading/deposit/{deposit_id}/withdraw — buy/withdraw an item."""
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/withdraw",
            json={"coin_value": coin_value}, priority=PRIORITY_HIGH)

    async def place_bid(self, deposit_id: int, bid_value: int, *,
                        fail_fast_429: bool = False) -> Any:
        """
        POST /trading/deposit/{deposit_id}/bid

        bid_value in coincents; must be at least 1% above the current bid
        (see ``min_next_bid``).

        fail_fast_429: raise immediately on 429 instead of waiting out the rate
        limit — auctions are time-sensitive, so a 60s wait wins nothing.

        Bids run at PRIORITY_BID: they jump ahead of background polling but yield
        to TradeBot's money actions when the shared rate-limit window is saturated.
        """
        return await self._request(
            "POST", f"/trading/deposit/{deposit_id}/bid",
            json={"bid_value": bid_value}, fail_fast_429=fail_fast_429,
            priority=PRIORITY_BID)

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
