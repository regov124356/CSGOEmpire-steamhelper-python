import base64
import json
import sys
import threading
import time

from steampy.client import SteamClient as SteampyClient
from steampy.exceptions import InvalidCredentials

from logger import logger

# Refresh the login this many seconds before the access token actually expires,
# so a request never goes out on an about-to-die token.
REFRESH_MARGIN = 300
# Fallback when the token's exp can't be read (format change): re-login once the
# session is at least this old, instead of re-logging in on every request.
MAX_TOKEN_AGE = 12 * 3600


class SteamClient(SteampyClient):
    """Steam-side operations only: login, trade offers, inventory tokens.

    steampy sets ``_access_token`` once at login and never refreshes it, and the
    logged-in session cookies expire with it. Rather than wrapping each call,
    ``__getattribute__`` ensures a live token (``_ensure_session``) before *any*
    public method runs, re-logging in only when the token is at/near expiry. So
    every steampy method that needs the webtoken/session — current or
    newly-used — is covered with no per-method boilerplate. The check is local
    (reads the JWT ``exp``); no network call unless a refresh is actually needed.

    CSGOEmpire API calls live in csgoempire_client.CSGOEmpireClient and the
    business logic wiring the two together lives in trade_bot.TradeBot.
    """

    # Public methods that must NOT trigger a token check: they either run *as
    # part of* the refresh (login) or are session-management primitives, so
    # wrapping them would recurse or fire before the session even exists.
    _NO_ENSURE = frozenset({"login", "logout", "is_session_alive"})

    def __init__(self, api_key: str, username: str, password: str, steam_guard: str):
        super().__init__(api_key=api_key)
        # Steam calls reach here via asyncio.to_thread and the Empire loop can
        # ask for the token concurrently with the offers loop; serialise refresh
        # so concurrent callers don't trigger overlapping re-logins.
        self._session_lock = threading.Lock()
        self._last_login = 0.0
        logger.info("Logging in to steam...")
        try:
            super().login(username=username, password=password, steam_guard=steam_guard)
            self._last_login = time.time()
            logger.info("Logged in.")
        except (InvalidCredentials, ValueError) as err:
            logger.error(f"Error: {err}")
            sys.exit()

    # ------------------------------------------------------------------ #
    # token freshness
    # ------------------------------------------------------------------ #
    @staticmethod
    def _jwt_exp(token: str) -> int | None:
        """Read the ``exp`` claim from a JWT access token without verifying it."""
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return int(payload["exp"])
        except (AttributeError, IndexError, ValueError, KeyError, TypeError):
            return None

    def _token_expired(self) -> bool:
        if not self._access_token:
            return True
        exp = self._jwt_exp(self._access_token)
        if exp is not None:
            return time.time() >= exp - REFRESH_MARGIN
        # exp unreadable -> fall back to age so we don't re-login every request.
        return time.time() - self._last_login >= MAX_TOKEN_AGE

    def _refresh_session(self) -> None:
        logger.info("Steam token near/at expiry; re-logging in...")
        # steampy logs in on the *same* requests.Session; cookies left from the
        # previous login (a 'sessionid' on multiple domains) make its finalize
        # step raise CookieConflictError. Clear them so the re-login is clean.
        self._session.cookies.clear()
        self.was_login_executed = False
        self.login(self.username, self._password, self.steam_guard_string)
        self._last_login = time.time()

    def _ensure_session(self) -> None:
        """Re-login before a request if the access token is at/near expiry.
        Cheap and lock-guarded; no network unless a refresh is actually needed."""
        with self._session_lock:
            if self._token_expired():
                self._refresh_session()

    # ------------------------------------------------------------------ #
    # generic interception: ensure a live token before any public method
    # ------------------------------------------------------------------ #
    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        # Only public callables; skip dunders/privates, plain attributes, and the
        # methods used during the refresh itself (see _NO_ENSURE).
        if name.startswith("_") or not callable(attr):
            return attr
        if name in SteamClient._NO_ENSURE:
            return attr

        def _with_session(*args, **kwargs):
            self._ensure_session()
            return attr(*args, **kwargs)

        return _with_session

    def get_access_token(self) -> str:
        """Fresh Steam access token (used by TradeBot to feed CSGOEmpire).
        The token is kept live by __getattribute__'s _ensure_session hook."""
        return self._access_token
