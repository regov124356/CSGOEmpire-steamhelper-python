"""
Thin async client for the CSFloat API.

Inherits the raw endpoints from csfloat_api.Client and overrides _request to add
the cross-cutting concerns every call needs:

    - a single pooled aiohttp session (the base library opens a new session per
      request, which kills connection reuse)
    - adaptive per-endpoint rate limiting driven by the x-ratelimit-* response
      headers (CSFloat limits each endpoint separately, e.g. /listings = 200/h
      while /listings/{id}/buy-orders = 20/min)
    - retries on transient failures (429, 5xx, network), honouring the reset time
    - typed CSFloatError instead of bare Exception

No pricing logic lives here. Computing prices, the divider conversion and the
refresh loop belong to price_service.PriceService.
"""

import asyncio
import re
import time
from typing import Optional

import aiohttp
from csfloat_api.csfloat_client import Client

API_URL = 'https://csfloat.com/api/v1'

_ID_SEGMENT = re.compile(r"^\d+$")


class CSFloatError(Exception):
    """Raised when a CSFloat request fails after exhausting retries."""

    def __init__(self, message: str, status: Optional[int] = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class _Bucket:
    """Live rate-limit state for one endpoint, learned from response headers."""
    __slots__ = ("limit", "remaining", "reset", "lock")

    def __init__(self):
        self.limit: Optional[int] = None
        self.remaining: Optional[int] = None
        self.reset: Optional[float] = None  # epoch seconds
        self.lock = asyncio.Lock()


class CSFloatClient(Client):
    # base Client uses __slots__; this subclass gets a __dict__, so the extra
    # instance attributes below are fine.
    def __init__(self, api_key: str, *, max_retries: int = 3, timeout: float = 30.0,
                 verify_ssl: bool = False):
        # Deliberately skip super().__init__: base Client (csfloat_api >=1.1.0)
        # eagerly opens a ClientSession + connector in its constructor, which we'd
        # immediately orphan (setting _session = None below) -> "Unclosed client
        # session" warning. We manage our own pooled session via _ensure_session,
        # so set up just the slots the base/our code reads instead.
        self.API_KEY = api_key
        self.proxy = None
        self._headers = {"Authorization": api_key}
        self.max_retries = max_retries
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._verify_ssl = verify_ssl
        self._session: Optional[aiohttp.ClientSession] = None
        self._buckets: dict[str, _Bucket] = {}

    # ------------------------------------------------------------------ #
    # session lifecycle
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "CSFloatClient":
        self._ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ #
    # rate-limit bucket handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bucket_key(method: str, parameters: str) -> str:
        """Normalise a path into a per-endpoint bucket key (ids -> {id})."""
        path = parameters.split("?", 1)[0]
        segments = ["{id}" if _ID_SEGMENT.match(s) else s for s in path.split("/")]
        return f"{method} {'/'.join(segments)}"

    @staticmethod
    def _read_headers(headers):
        try:
            return (int(headers["x-ratelimit-limit"]),
                    int(headers["x-ratelimit-remaining"]),
                    float(headers["x-ratelimit-reset"]))
        except (KeyError, ValueError):
            return None

    def _update_bucket(self, bucket: _Bucket, headers) -> None:
        parsed = self._read_headers(headers)
        if parsed:
            bucket.limit, bucket.remaining, bucket.reset = parsed

    @staticmethod
    async def _pace(bucket: _Bucket) -> None:
        """Spread the remaining quota evenly across the window to avoid 429s."""
        if bucket.reset is None or bucket.remaining is None:
            return
        now = time.time()
        if bucket.reset <= now:
            return
        if bucket.remaining <= 0:
            await asyncio.sleep(bucket.reset - now)
        else:
            await asyncio.sleep((bucket.reset - now) / bucket.remaining)

    # ------------------------------------------------------------------ #
    # core request (overrides csfloat_api.Client._request)
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, parameters: str, json_data=None) -> Optional[dict]:
        if method not in self._SUPPORTED_METHODS:
            raise ValueError('Unsupported HTTP method.')

        url = f"{API_URL}{parameters}"
        bucket = self._buckets.setdefault(self._bucket_key(method, parameters), _Bucket())
        session = self._ensure_session()
        attempt = 0

        async with bucket.lock:
            while True:
                await self._pace(bucket)
                try:
                    async with session.request(method, url, ssl=self._verify_ssl,
                                               json=json_data) as resp:
                        self._update_bucket(bucket, resp.headers)

                        if resp.status == 429:
                            if attempt >= self.max_retries:
                                raise CSFloatError("Rate limited, retries exhausted", 429)
                            attempt += 1
                            continue  # _pace waits until reset (remaining <= 0)

                        if resp.status >= 500:
                            if attempt >= self.max_retries:
                                raise CSFloatError(f"Server error {resp.status}",
                                                   resp.status, await self._safe_body(resp))
                            attempt += 1
                            await asyncio.sleep(2 ** attempt)
                            continue

                        if resp.status != 200:
                            message = self.ERROR_MESSAGES.get(resp.status, f"HTTP {resp.status}")
                            raise CSFloatError(message, resp.status, await self._safe_body(resp))

                        if resp.content_type != 'application/json':
                            raise CSFloatError(f"Expected JSON, got {resp.content_type}",
                                               resp.status)

                        return await resp.json()

                except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                    if attempt >= self.max_retries:
                        raise CSFloatError(f"Network error: {err}") from err
                    attempt += 1
                    await asyncio.sleep(2 ** attempt)

    @staticmethod
    async def _safe_body(resp):
        try:
            return await resp.json()
        except Exception:
            return await resp.text()
