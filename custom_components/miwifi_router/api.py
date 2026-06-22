"""MiWiFi Router API Client with stok session management.

Key design decisions (inspired by dmamontov/hass-miwifi patterns):
- Stok (session token) is kept alive as long as the router accepts it.
  We do NOT expire the stok locally — the router maintains server-side session
  state and will return 401 when the session truly expires. Only then do we
  re-login.
- Login uses a FRESH aiohttp session to avoid stale cookie interference.
- Before the FIRST login, we call logout() to clear any stale server-side
  session from a previous run (e.g., from config_flow or HA restart).
- On login failure with one hash algorithm, we try the OTHER algorithm
  once (since "not auth" per dmamontov Issue #62 means wrong hash, not
  session conflict — retrying the same hash is pointless).
- Hash algorithm auto-detection:
  Before login, we try to read init_info WITHOUT authentication from TWO
  URL paths:
    1. /api/xqsystem/init_info              (new firmware, e.g. BE5000)
    2. /cgi-bin/luci/api/xqsystem/init_info (old firmware, e.g. AX3600)
  If it contains newEncryptMode=1, the router uses SHA256+SHA256 — we try
  SHA256 first.
  Otherwise (newEncryptMode=0 or missing, OR init_info unreachable on both
  paths), the router uses SHA1+SHA1 — we try SHA1 first.
  On login failure, we automatically try the OTHER algorithm once.
  Switching is NOT tied to error message text (some firmware returns
  English "not auth" instead of Chinese "密码错误"; per dmamontov Issue #62,
  "not auth" simply means "wrong password/hash", not session conflict).
  Total: at most 2 HTTP login requests (1 per algorithm).
  User can also force a specific algorithm via force_hash_algo to skip
  auto-detection and fallback entirely.

v1.3.10: Revert nonce format to 4-part (regression fix).
  Symptom (from user HA log 2026-06-17 20:00):
  - v1.3.6 user with router at 172.16.1.1 was working fine
  - After upgrading to v1.3.9, login failed with 'not auth' on BOTH
    SHA256 and SHA1 algorithms
  - Root cause: v1.3.9 changed nonce format from 4 parts
    (0_MAC_TS_RAND) to 5 parts (0_MAC_TS_RAND_COUNTER), and this
    router strictly validates nonce format — rejects 5-part nonces
    with 'not auth' (not even 'Invalid nonce', which made it look
    like a hash problem).
  Fix:
  - Revert nonce to 4-part format: 0_MAC_TIMESTAMP_RANDOM
    (matches dmamontov/hass-miwifi and router's JS Encrypt.nonceCreat)
  - Keep real client MAC from uuid.getnode() (this part didn't cause
    the regression and is more correct than the placeholder)
  - Keep the code=1582 Invalid nonce retry logic in _login() — this
    still handles the rare case where two logins in the same second
    produce identical timestamps. The router will reject the second
    one with code=1582, we wait 2s and retry (timestamp advances).
  - Removed _nonce_counter class variable (no longer needed).

v1.3.9: Fix "Invalid nonce" error caused by rapid re-login.
  Symptom (from user HA log 2026-06-17):
  - test_connection() logged in successfully, then logged out
  - 300ms later, coordinator's first refresh tried to login again
  - Both nonces shared the same Unix second timestamp
  - Router rejected the second one with {'code': 1582, 'msg': 'Invalid nonce'}
  - Old code then wrongly switched to SHA256 (which also failed)
  - Eventually succeeded on a 3rd attempt after creating a fresh client
  Fix:
  - Use real client MAC in nonce (uuid.getnode()) instead of placeholder
    "00:00:00:00:00:00". Matches what router's JS Encrypt.oldPwd() does,
    and matches dmamontov/hass-miwifi behavior.
  - Recognize code=1582 "Invalid nonce" specifically: this is NOT a wrong
    hash problem, so DON'T switch algorithm. Instead, wait 2s for router
    to release the previous session, then retry the SAME algorithm.
  - Add a class-level nonce counter to guarantee uniqueness within same
    second (extra entropy beyond timestamp + random).
  - Demote all [DEBUG]-tagged log statements from warning to debug level.
    Users who want detailed login diagnostics should enable debug logging
    for the miwifi_router integration in HA's logger config.

v1.3.7-debug: Enhanced debug logging for login authentication troubleshooting.
  Every step of the login process now logs detailed info including:
  - nonce generation, inner/outer hash computation
  - full response bodies from init_info and login
  - cookie headers and redirect tracking
  - hash algorithm detection and switching details

v1.3.8: Fix critical login failure on old-firmware routers.
  Root cause (from user HA log 2026-06-16):
  - Router returned `{'code': 401, 'msg': 'not auth'}` for SHA256 login attempts
  - Old code only switched to SHA1 when msg contained "密码错误" (Chinese),
    but router returned English "not auth" → SHA1 fallback never triggered
  - init_info URL only tried /api/xqsystem/init_info (404 on old firmware),
    not /cgi-bin/luci/api/xqsystem/init_info
  Fix (per dmamontov/hass-miwifi Issue #62 analysis):
  - Try BOTH init_info URL paths (with and without /cgi-bin/luci/ prefix)
  - Trust init_info detection as first attempt:
    * newEncryptMode=1 -> try SHA256 first, then SHA1
    * newEncryptMode!=1 or init_info unreachable -> try SHA1 first, then SHA256
  - Each algorithm is tried AT MOST ONCE (not auth = wrong hash, retrying
    same hash is pointless). Total: 2 HTTP requests max.
  - Switching is NO LONGER tied to error message text.
  - Added optional force_hash_algo parameter for manual override (locks
    algorithm, no fallback).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
import uuid
from typing import Any

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_DEVICE_LIST,
    API_INIT_INFO,
    API_LOGIN,
    API_LOGOUT,
    API_REBOOT,
    API_NEWSTATUS,
    API_STATUS,
    API_SYSTEM_STATUS,
    PUBLIC_KEY,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
DEFAULT_CALL_DELAY = 1  # seconds to wait after logout before re-login
MAX_LOGIN_RETRIES = 2  # Total: 2 algorithms × ATTEMPTS_PER_ALGO
ATTEMPTS_PER_ALGO = 1  # Attempts per hash algorithm before switching
# Router returns code=1582 "Invalid nonce" when:
#   - Two logins share the same nonce within the same second
#   - The previous session is still being torn down server-side
# When we see 1582, we DON'T switch algorithm (it's not a hash problem).
# Instead, we wait NONCE_RETRY_DELAY seconds and retry the SAME algorithm.
INVALID_NONCE_CODE = 1582
NONCE_RETRY_DELAY = 2  # seconds to wait before retrying on Invalid nonce
MAX_NONCE_RETRIES = 2  # max retries on Invalid nonce per algorithm


class MiWiFiAuthError(Exception):
    """Authentication error."""


class MiWiFiConnectionError(Exception):
    """Connection error."""


class MiWiFiAPIClient:
    """API client for MiWiFi router with persistent stok session.

    Strategy: keep using the stok until the router rejects it (401/403).
    This leverages the router's server-side session persistence and avoids
    the "not auth" problem that occurs when re-logging in while the old
    session is still alive on the router side.
    """

    def __init__(self, host: str, password: str, hass=None, force_hash_algo: str | None = None) -> None:
        self._host = host
        self._password = password
        self._base_url = f"http://{host}"
        self._hass = hass
        # Stok is kept indefinitely until router rejects it
        self._stok: str | None = None
        # Track whether this is the first login attempt
        self._is_first_login: bool = True
        # Router hardware info
        self._init_info_cache: dict[str, Any] | None = None
        self._init_info_expire: float = 0
        self._mac: str | None = None
        self._model: str | None = None
        self._firmware: str | None = None
        self._force_hash_algo = force_hash_algo  # "SHA1" | "SHA256" | None
        self.__init_hash_algo()
        _LOGGER.debug(
            "[DEBUG] MiWiFiAPIClient created for %s | public_key=%s | force_hash_algo=%s",
            host, PUBLIC_KEY, force_hash_algo,
        )

    def _get_session(self) -> aiohttp.ClientSession:
        """Get HA's shared aiohttp client session for authenticated requests."""
        if self._hass is not None:
            return async_get_clientsession(self._hass)
        raise RuntimeError("Home Assistant instance not provided")

    async def close(self) -> None:
        """Clean up - logout if we have an active session."""
        if self._stok:
            try:
                await self.logout()
            except Exception as err:
                _LOGGER.debug("Logout during close failed (non-fatal): %s", err)

    # ---- Authentication ----

    # Hash algorithm combos to try: (name, inner_func, outer_func)
    # Newer firmware (BE5000 etc.) uses SHA256+SHA256
    # Older firmware (AX3600, AC2100 etc.) uses SHA1+SHA1
    _HASH_ALGORITHMS = [
        ("SHA256", hashlib.sha256),
        ("SHA1", hashlib.sha1),
    ]

    def __init_hash_algo(self) -> None:
        """Initialize hash algorithm.

        Priority:
          1. force_hash_algo (if set by user) — locks the algorithm, no auto-detection
          2. Otherwise default to SHA1 (most old firmware uses SHA1; new firmware
             typically exposes init_info without auth, so detection succeeds for them
             and we'll switch to SHA256 before login)
        """
        if self._force_hash_algo in ("SHA1", "SHA256"):
            self._hash_algo_name: str = self._force_hash_algo
            self._hash_algo = hashlib.sha1 if self._force_hash_algo == "SHA1" else hashlib.sha256
            self._hash_algo_detected: bool = True  # User forced it, treat as detected
            _LOGGER.debug(
                "[DEBUG] Hash algo FORCED to %s by user config (auto-detection disabled)",
                self._hash_algo_name,
            )
        else:
            # Default to SHA1 — covers most old firmware routers (AX3600, AC2100, etc.)
            # New firmware routers typically expose init_info without auth, so
            # _detect_hash_algo_from_init_info will switch us to SHA256 before login.
            self._hash_algo_name = "SHA1"
            self._hash_algo = hashlib.sha1
            self._hash_algo_detected = False
            _LOGGER.debug(
                "[DEBUG] Hash algo initialized: default=SHA1 (will be overridden by init_info if available)",
            )
        self._hash_algos_tried: set[str] = set()

    def _switch_hash_algo(self) -> bool:
        """Switch to the OTHER hash algorithm (SHA256<->SHA1).

        Unlike a sequential "next", this always switches to the alternative
        algorithm regardless of current position. This handles the case where
        init_info detected SHA1 but login still fails — we should try SHA256
        as fallback, not give up.

        Returns True if switched, False if both algorithms have been tried.
        """
        old_name = self._hash_algo_name
        self._hash_algos_tried.add(self._hash_algo_name)

        _LOGGER.debug(
            "[DEBUG] _switch_hash_algo called for %s | current=%s | tried=%s",
            self._host, old_name, self._hash_algos_tried,
        )

        # Find an algorithm we haven't tried yet
        for name, algo in self._HASH_ALGORITHMS:
            if name not in self._hash_algos_tried:
                self._hash_algo_name = name
                self._hash_algo = algo
                _LOGGER.debug(
                    "[DEBUG] Switching hash algorithm from %s to %s for %s",
                    old_name, name, self._host,
                )
                return True

        _LOGGER.debug(
            "[DEBUG] All hash algorithms tried for %s: %s",
            self._host, self._hash_algos_tried,
        )
        return False  # All algorithms tried

    @staticmethod
    def _hash(text: str, algo) -> str:
        """Hash text using the specified algorithm."""
        result = algo(text.encode("utf-8")).hexdigest()
        return result

    def _build_login_password(self, nonce: str) -> str:
        """Build the login password hash.

        Algorithm: hash(nonce + hash(password + public_key))
        Newer routers use SHA256, older routers use SHA1.
        """
        inner_input = self._password + PUBLIC_KEY
        inner_hash = self._hash(inner_input, self._hash_algo)
        outer_input = nonce + inner_hash
        outer_hash = self._hash(outer_input, self._hash_algo)
        _LOGGER.debug(
            "[DEBUG] _build_login_password for %s | algo=%s | "
            "inner_input_len=%d | inner_hash=%s | "
            "outer_input_len=%d | outer_hash=%s",
            self._host, self._hash_algo_name,
            len(inner_input), inner_hash,
            len(outer_input), outer_hash,
        )
        return outer_hash

    @staticmethod
    def _get_client_mac() -> str:
        """Get the local machine's MAC address for use in nonce.

        Uses uuid.getnode() which returns the MAC as an integer.
        Falls back to "00:00:00:00:00:00" if getnode() fails.

        This matches the behavior of dmamontov/hass-miwifi and the
        router's own JS Encrypt.oldPwd() which uses the browser/device MAC.
        """
        try:
            node = uuid.getnode()
            as_hex = f"{node:012x}"
            return ":".join(as_hex[i:i + 2] for i in range(0, 12, 2))
        except Exception:
            return "00:00:00:00:00:00"

    @classmethod
    def _generate_nonce(cls) -> str:
        """Generate a nonce for login.

        Format: {type}_{mac}_{timestamp}_{random}  (4 parts)

        Matches the format used by dmamontov/hass-miwifi and the router's
        own JS Encrypt.nonceCreat(). Some routers strictly validate the
        nonce format (e.g. reject 5-part nonces), so we MUST stay with
        the 4-part format that has been working since v1.0.

        - mac: real client MAC (from uuid.getnode())
        - timestamp: Unix seconds
        - random: 1000-9999

        If two logins happen within the same second, they may generate
        the same timestamp portion of the nonce. This is handled by the
        code=1582 "Invalid nonce" retry logic in _login() — the router
        will reject the second one, we wait 2s, then retry the SAME
        algorithm with a fresh nonce (the timestamp will have advanced
        by then).
        """
        nonce_type = 0
        mac = cls._get_client_mac()
        now = int(time.time())
        rand = random.randint(1000, 9999)
        nonce = f"{nonce_type}_{mac}_{now}_{rand}"
        _LOGGER.debug("[DEBUG] Generated nonce: %s", nonce)
        return nonce

    async def _ensure_stok(self) -> str:
        """Ensure we have a stok. Only login if we don't have one yet.

        We do NOT expire the stok locally. The router keeps the server-side
        session alive, and we keep using the same stok until the router
        rejects it with 401/403. At that point, _api_get() will clear the
        stok and call this method again to get a new one.
        """
        if self._stok:
            return self._stok

        _LOGGER.debug("No stok available, logging in to %s", self._host)
        await self._login()
        return self._stok  # type: ignore[return-value]

    async def logout(self) -> None:
        """Logout from the router to clear the server-side session.

        This is important before the first login to avoid "not auth" conflicts
        caused by stale sessions from previous runs or config_flow testing.
        """
        if not self._stok:
            _LOGGER.debug("No stok to logout with for %s", self._host)
            return

        logout_url = (
            f"{self._base_url}/cgi-bin/luci/;stok={self._stok}{API_LOGOUT}"
        )
        try:
            # Use a fresh session for logout to avoid cookie interference
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            ) as session:
                async with session.get(
                    logout_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=False,
                ) as resp:
                    _LOGGER.debug(
                        "Logout from %s: status=%s", self._host, resp.status,
                    )
        except Exception as err:
            _LOGGER.debug("Logout request failed (non-fatal): %s", err)

        self._stok = None
        _LOGGER.debug("Stok cleared for %s after logout", self._host)

    async def _detect_hash_algo_from_init_info(self) -> None:
        """Try to detect the hash algorithm from init_info without authentication.

        Some routers allow unauthenticated access to init_info. The URL path
        differs between firmware versions:
          - New firmware (BE5000, etc.):  /api/xqsystem/init_info
          - Old firmware (AX3600, etc.): /cgi-bin/luci/api/xqsystem/init_info

        We try BOTH paths. The newEncryptMode field determines the hash algorithm:
          - newEncryptMode=1 -> SHA256+SHA256 (new firmware explicitly declares this)
          - Field missing or any other value -> SHA1+SHA1 (old firmware default)

        If neither path is accessible without auth, we keep the default (SHA1)
        and rely on the login retry strategy to try the other algorithm.
        """
        if self._hash_algo_detected:
            _LOGGER.debug(
                "[DEBUG] Hash algo already detected/forced as %s for %s, skipping init_info",
                self._hash_algo_name, self._host,
            )
            return  # Already detected or forced by user

        # Try both URL paths — old firmware uses /cgi-bin/luci/ prefix, new firmware does not
        urls_to_try = [
            f"{self._base_url}{API_INIT_INFO}",                          # /api/xqsystem/init_info
            f"{self._base_url}/cgi-bin/luci{API_INIT_INFO}",             # /cgi-bin/luci/api/xqsystem/init_info
        ]

        for url in urls_to_try:
            try:
                _LOGGER.debug("[DEBUG] Fetching init_info (no auth) from %s", url)
                async with aiohttp.ClientSession(
                    cookie_jar=aiohttp.CookieJar(unsafe=True),
                ) as session:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=5),
                        allow_redirects=True,
                    ) as resp:
                        _LOGGER.debug(
                            "[DEBUG] init_info response: status=%s, content_type=%s for %s (url=%s)",
                            resp.status, resp.content_type, self._host, url,
                        )
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            _LOGGER.debug(
                                "[DEBUG] init_info full response for %s: %s",
                                self._host,
                                str(data)[:500],
                            )
                            encrypt_mode = data.get("newEncryptMode")
                            _LOGGER.debug(
                                "[DEBUG] init_info newEncryptMode=%s for %s",
                                encrypt_mode, self._host,
                            )
                            if encrypt_mode == 1:
                                # New firmware explicitly declares SHA256
                                self._hash_algo_name = "SHA256"
                                self._hash_algo = hashlib.sha256
                                _LOGGER.debug(
                                    "[DEBUG] Detected newEncryptMode=1 -> SHA256 for %s",
                                    self._host,
                                )
                            else:
                                # Old firmware: field missing or other value -> SHA1
                                self._hash_algo_name = "SHA1"
                                self._hash_algo = hashlib.sha1
                                _LOGGER.debug(
                                    "[DEBUG] Detected newEncryptMode=%s -> SHA1 for %s",
                                    encrypt_mode, self._host,
                                )
                            self._hash_algo_detected = True
                            return  # Got it, no need to try other URL
                        else:
                            resp_text = await resp.text()
                            _LOGGER.debug(
                                "[DEBUG] init_info returned status %s for %s (url=%s), body: %s",
                                resp.status, self._host, url, resp_text[:300],
                            )
                            # Try next URL path
            except Exception as err:
                _LOGGER.debug(
                    "[DEBUG] Could not read init_info at %s for %s: %s",
                    url, self._host, err,
                )
                # Try next URL path

        _LOGGER.debug(
            "[DEBUG] All init_info URL paths failed for %s, keeping default algo=%s",
            self._host, self._hash_algo_name,
        )

    async def _login(self) -> None:
        """Authenticate with the router and cache the stok.

        Login flow (v1.3.9):
        1. Detect hash algorithm from init_info (try BOTH URL paths).
           - newEncryptMode=1 detected -> current_algo = SHA256
           - newEncryptMode!=1 OR init_info unreachable -> current_algo = SHA1
        2. On FIRST login ever, call logout() to clear any stale session,
           then wait 1s.
        3. Try current algorithm ONCE.
        4. If it fails with code=1582 "Invalid nonce": DON'T switch algorithm
           (this is not a hash problem). Wait 2s for the router to release the
           previous session, then retry the SAME algorithm.
           Up to MAX_NONCE_RETRIES retries per algorithm.
        5. If it fails with other errors (e.g. "not auth" = wrong hash),
           switch to the OTHER algorithm and try ONCE (with same nonce retry
           behavior if 1582 occurs).
           *** Switching is NOT tied to error message text. Per dmamontov
           Issue #62, "not auth" means wrong hash, retrying same hash is
           pointless. ***
        6. If user set force_hash_algo, only that algorithm is tried (no
           fallback to other algorithm, but Invalid nonce retry still applies).
        7. Extract stok from response on success.

        Total: at most 2 algorithms × (1 + MAX_NONCE_RETRIES) HTTP requests
        when not forced; (1 + MAX_NONCE_RETRIES) when forced.

        The stok is kept indefinitely (no local expiry). The router maintains
        the session server-side. When the router eventually invalidates it,
        we'll get a 401 and re-login then.
        """
        _LOGGER.debug(
            "[DEBUG] _login() called for %s | first_login=%s | current_algo=%s | detected=%s | forced=%s",
            self._host, self._is_first_login, self._hash_algo_name,
            self._hash_algo_detected, self._force_hash_algo is not None,
        )

        # Step 1: Try to detect hash algorithm from init_info (skipped if user forced)
        await self._detect_hash_algo_from_init_info()

        # On first login, clear any stale session from previous runs
        if self._is_first_login:
            _LOGGER.debug("[DEBUG] First login for %s, clearing stale session", self._host)
            try:
                await self.logout()
            except Exception as err:
                _LOGGER.debug("Pre-login logout failed (non-fatal): %s", err)
            await asyncio.sleep(DEFAULT_CALL_DELAY)
            self._is_first_login = False

        # Step 2: Build algorithm order
        # - If user forced: only try that ONE algorithm
        # - Otherwise: try detected/default first, then the OTHER as fallback
        if self._force_hash_algo in ("SHA1", "SHA256"):
            algo_order = [self._hash_algo_name]
            _LOGGER.debug(
                "[DEBUG] Hash algo FORCED to %s, no algorithm fallback for %s",
                self._hash_algo_name, self._host,
            )
        else:
            other_algo = "SHA1" if self._hash_algo_name == "SHA256" else "SHA256"
            algo_order = [self._hash_algo_name, other_algo]
            _LOGGER.debug(
                "[DEBUG] Login algo order for %s: %s (try each ONCE, total %d attempts, "
                "plus up to %d nonce retries per algo on code 1582)",
                self._host, algo_order, len(algo_order), MAX_NONCE_RETRIES,
            )

        # Step 3: Try each algorithm in order
        # Within each algorithm, retry on Invalid nonce (code 1582) up to
        # MAX_NONCE_RETRIES times. Other errors -> switch to next algorithm.
        last_error_code = None
        last_error_msg = ""
        last_response: dict[str, Any] = {}
        attempt = 0  # global attempt counter for logging

        for algo_idx, algo_name in enumerate(algo_order):
            # Switch algorithm if needed (after first iteration)
            if algo_idx > 0:
                _LOGGER.debug(
                    "[DEBUG] Previous algorithm %s failed for %s, switching to %s as fallback",
                    algo_order[algo_idx - 1], self._host, algo_name,
                )
                self._hash_algo_name = algo_name
                self._hash_algo = hashlib.sha1 if algo_name == "SHA1" else hashlib.sha256
                # Brief delay between algorithm switches
                await asyncio.sleep(1)

            # Try this algorithm, with retries on Invalid nonce
            nonce_retry = 0
            while True:
                attempt += 1
                _LOGGER.debug(
                    "[DEBUG] Login attempt %d for %s with algo=%s (algo_idx=%d, nonce_retry=%d/%d)",
                    attempt, self._host, self._hash_algo_name,
                    algo_idx, nonce_retry, MAX_NONCE_RETRIES,
                )

                data = await self._do_login_request(attempt)
                last_response = data

                _LOGGER.debug(
                    "[DEBUG] Login attempt %d response for %s: code=%s, msg=%s, full=%s",
                    attempt, self._host, data.get("code"), data.get("msg", ""),
                    str(data)[:500],
                )

                if data.get("code") == 0:
                    _LOGGER.debug(
                        "[DEBUG] Login SUCCESS for %s on attempt %d with algo=%s",
                        self._host, attempt, self._hash_algo_name,
                    )
                    self._hash_algo_detected = True
                    # Extract stok
                    url = data.get("url", "")
                    if ";stok=" in url:
                        self._stok = url.split(";stok=")[1].split("/")[0]
                    else:
                        self._stok = data.get("token", "")

                    if not self._stok:
                        _LOGGER.error(
                            "Could not extract stok from login response: %s", data,
                        )
                        raise MiWiFiConnectionError(
                            "Could not extract stok from login response"
                        )

                    _LOGGER.info(
                        "Successfully logged in to MiWiFi router at %s using %s "
                        "(stok will be reused until router rejects it)",
                        self._host, self._hash_algo_name,
                    )

                    # After successful login, try to get router info
                    await self._fetch_router_info_after_login()
                    return  # Success!

                # Failed — record error
                last_error_code = data.get("code")
                last_error_msg = str(data.get("msg", ""))

                # Special case: Invalid nonce (code 1582)
                # This is NOT a hash problem — it means the router rejected
                # our nonce because either:
                #   (a) we generated two nonces within the same second, OR
                #   (b) a previous session is still being torn down
                # Solution: wait and retry the SAME algorithm with a fresh nonce.
                if last_error_code == INVALID_NONCE_CODE:
                    if nonce_retry < MAX_NONCE_RETRIES:
                        nonce_retry += 1
                        _LOGGER.debug(
                            "[DEBUG] Got Invalid nonce (code 1582) for %s with algo=%s, "
                            "waiting %ds before retry (nonce_retry=%d/%d)",
                            self._host, self._hash_algo_name,
                            NONCE_RETRY_DELAY, nonce_retry, MAX_NONCE_RETRIES,
                        )
                        await asyncio.sleep(NONCE_RETRY_DELAY)
                        continue  # retry same algorithm with fresh nonce
                    else:
                        _LOGGER.debug(
                            "[DEBUG] Invalid nonce retries exhausted for %s with algo=%s "
                            "(tried %d times), moving on",
                            self._host, self._hash_algo_name, nonce_retry,
                        )
                        break  # move to next algorithm (or fail)
                else:
                    # Other error (e.g. "not auth" = wrong hash) — don't retry,
                    # move to next algorithm immediately
                    _LOGGER.debug(
                        "[DEBUG] Algorithm %s failed for %s: code=%s msg='%s' (not retryable)",
                        algo_name, self._host, last_error_code, last_error_msg,
                    )
                    break  # move to next algorithm

        # All algorithms and nonce retries exhausted
        _LOGGER.debug(
            "[DEBUG] Login failed for %s after %d attempt(s) across %d algorithm(s): "
            "code=%s, msg=%s, last_response=%s",
            self._host, attempt, len(algo_order),
            last_error_code, last_error_msg, str(last_response)[:500],
        )

        # Provide a more helpful error message
        if last_error_code == 401:
            raise MiWiFiAuthError(
                f"Authentication failed: {last_error_msg} "
                f"(tried algorithms: {algo_order}, last code: {last_error_code})"
            )
        raise MiWiFiConnectionError(
            f"Login failed: {last_error_msg} (code: {last_error_code})"
        )

    async def _do_login_request(self, attempt: int = 1) -> dict[str, Any]:
        """Send a single login POST request and return the response dict.

        This is called by _login() which handles retries on session conflict.
        """
        nonce = self._generate_nonce()
        password = self._build_login_password(nonce)
        login_url = f"{self._base_url}{API_LOGIN}"

        _LOGGER.debug(
            "[DEBUG] _do_login_request attempt %d for %s | algo=%s | "
            "nonce=%s | pwd_hash_len=%d | login_url=%s",
            attempt, self._host, self._hash_algo_name,
            nonce, len(password), login_url,
        )

        # Use a fresh session for login to avoid stale cookie interference
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            connector=aiohttp.TCPConnector(force_close=True),
        ) as login_session:
            # Step 1: GET root page to clear stale server-side session
            try:
                async with login_session.get(
                    self._base_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=True,
                ) as pre_resp:
                    _LOGGER.debug(
                        "[DEBUG] Pre-login GET status: %s for %s (clearing stale session)",
                        pre_resp.status, self._host,
                    )
            except Exception as pre_err:
                _LOGGER.debug("Pre-login GET failed (non-fatal): %s", pre_err)

            # Step 2: POST login data
            try:
                post_data = {
                    "username": "admin",
                    "password": password,
                    "logtype": "2",
                    "nonce": nonce,
                }
                _LOGGER.debug(
                    "[DEBUG] POST login for %s | username=admin | logtype=2 | "
                    "nonce=%s | password_hash=%s",
                    self._host, nonce, password,
                )
                async with login_session.post(
                    login_url,
                    data=post_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as resp:
                    _LOGGER.debug(
                        "[DEBUG] Login response status: %s for %s",
                        resp.status, self._host,
                    )

                    if resp.status != 200:
                        resp_text = await resp.text()
                        _LOGGER.debug(
                            "[DEBUG] Login HTTP error for %s: status %s, body: %s",
                            self._host, resp.status, resp_text[:300],
                        )
                        raise MiWiFiConnectionError(
                            f"Login HTTP error: status {resp.status}"
                        )

                    data = await resp.json(content_type=None)
                    _LOGGER.debug(
                        "[DEBUG] Login response JSON for %s: %s",
                        self._host, str(data)[:500],
                    )
                    return data

            except aiohttp.ClientError as err:
                _LOGGER.debug("[DEBUG] Cannot connect to router at %s: %s", self._host, err)
                raise MiWiFiConnectionError(
                    f"Cannot connect to router: {err}"
                ) from err
            except MiWiFiConnectionError:
                raise
            except Exception as err:
                _LOGGER.debug("[DEBUG] Unexpected login error for %s: %s", self._host, err)
                raise MiWiFiConnectionError(f"Login error: {err}") from err

    async def _fetch_router_info_after_login(self) -> None:
        """After login, fetch router hardware info from available endpoints."""
        session = self._get_session()

        # Try newstatus endpoint first - it has hardware info
        try:
            url = f"{self._base_url}/cgi-bin/luci/;stok={self._stok}{API_NEWSTATUS}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    hardware = data.get("hardware", {})
                    if isinstance(hardware, dict) and hardware:
                        if hardware.get("displayName"):
                            self._model = hardware["displayName"]
                        if hardware.get("version"):
                            self._firmware = hardware["version"]
                        if hardware.get("mac"):
                            self._mac = hardware["mac"]
                    _LOGGER.debug(
                        "Got router info from newstatus: model=%s, firmware=%s",
                        self._model, self._firmware,
                    )
                    return
        except Exception as err:
            _LOGGER.debug("Could not fetch router info from newstatus: %s", err)

        # Fallback: try init_info endpoint
        try:
            url = f"{self._base_url}/cgi-bin/luci/;stok={self._stok}{API_INIT_INFO}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    self._mac = data.get("mac", self._mac)
                    hardware = data.get("hardware", {})
                    if not isinstance(hardware, dict):
                        hardware = {}
                    if hardware.get("displayName"):
                        self._model = hardware["displayName"]
                    if hardware.get("version"):
                        self._firmware = hardware["version"]
                    _LOGGER.debug(
                        "Got router info from init_info: model=%s", self._model
                    )
        except Exception as err:
            _LOGGER.debug("Could not fetch router info from init_info: %s", err)

    async def _api_get(self, endpoint: str) -> dict[str, Any]:
        """Make an authenticated API GET request using stok in URL.

        If the router rejects the request with 401/403 (stok expired),
        we clear the stok, re-login, and retry the request once.
        """
        stok = await self._ensure_stok()
        session = self._get_session()
        url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    # Stok rejected by router - re-login and retry
                    _LOGGER.info(
                        "Stok rejected (status %s) for %s, re-authenticating",
                        resp.status, endpoint,
                    )
                    self._stok = None
                    stok = await self._ensure_stok()
                    url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp2:
                        if resp2.status != 200:
                            resp_text = await resp2.text()
                            _LOGGER.error(
                                "API retry failed: %s returned status %s, body: %s",
                                endpoint, resp2.status, resp_text[:200],
                            )
                            raise MiWiFiConnectionError(
                                f"API error: {endpoint} returned status {resp2.status}"
                            )
                        data = await resp2.json(content_type=None)
                elif resp.status != 200:
                    resp_text = await resp.text()
                    _LOGGER.error(
                        "API error: %s returned status %s, body: %s",
                        endpoint, resp.status, resp_text[:200],
                    )
                    raise MiWiFiConnectionError(
                        f"API error: {endpoint} returned status {resp.status}"
                    )
                else:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            self._stok = None
            raise MiWiFiConnectionError(f"API request failed: {err}") from err

        # Check for auth errors in response body (some endpoints return 200
        # but with code=401 in the JSON body)
        if isinstance(data, dict):
            code = data.get("code")
            if code == 401:
                _LOGGER.info(
                    "Stok expired (code 401 in body) for %s, re-authenticating",
                    endpoint,
                )
                self._stok = None
                stok = await self._ensure_stok()
                url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"
                try:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp_retry:
                        if resp_retry.status != 200:
                            raise MiWiFiConnectionError(
                                f"API retry failed: {endpoint} returned status {resp_retry.status}"
                            )
                        data = await resp_retry.json(content_type=None)
                except aiohttp.ClientError as err:
                    self._stok = None
                    raise MiWiFiConnectionError(
                        f"API retry failed: {err}"
                    ) from err

        return data

    # ---- Public API Methods ----

    async def get_status(self) -> dict[str, Any]:
        """Get realtime router status (speeds, counts, device list with speeds).

        Combines data from /api/misystem/status and /api/xqsystem/status.
        """
        data = await self._api_get(API_STATUS)

        result: dict[str, Any] = {
            "wan": {},
            "count": {},
            "cpu": {},
            "mem": {},
            "dev": [],
        }

        # Extract WAN data from /api/misystem/status
        wan = data.get("wan", {})
        result["wan"] = {
            "downspeed": int(wan.get("downspeed", 0)),
            "upspeed": int(wan.get("upspeed", 0)),
            "download": int(wan.get("download", 0)),
            "upload": int(wan.get("upload", 0)),
        }

        # Extract device count
        count = data.get("count", {})
        if isinstance(count, dict):
            result["count"] = {
                "online": int(count.get("online", 0)),
                "all": int(count.get("all", 0)),
            }
        elif isinstance(count, (int, str)):
            result["count"] = {"online": int(count), "all": int(count)}

        # Extract CPU data
        cpu = data.get("cpu", {})
        if isinstance(cpu, dict):
            result["cpu"] = {
                "load": float(cpu.get("load", 0)),
                "core": int(cpu.get("core", 0)),
                "hz": cpu.get("hz", "0MHz"),
            }

        # Extract memory data
        mem = data.get("mem", {})
        if isinstance(mem, dict):
            result["mem"] = {
                "usage": float(mem.get("usage", 0)),
                "total": mem.get("total", "0MB"),
            }

        # Extract device list with per-device speeds
        dev_list = data.get("dev", [])
        devices = []
        for d in dev_list:
            device = {
                "mac": d.get("mac", "").upper(),
                "name": d.get("devname", d.get("mac", "")),
                "online": int(d.get("online", 0)),
                "upspeed": int(d.get("upspeed", 0)),
                "downspeed": int(d.get("downspeed", 0)),
                "upload": int(d.get("upload", 0)),
                "download": int(d.get("download", 0)),
                "maxuploadspeed": int(d.get("maxuploadspeed", 0)),
                "maxdownloadspeed": int(d.get("maxdownloadspeed", 0)),
                "isap": int(d.get("isap", 0)),
                "ip": d.get("ip", ""),
                "authority": d.get("authority", ""),
            }
            devices.append(device)
        result["dev"] = devices

        # If WAN data is empty from misystem/status, try xqsystem/status
        if not result["wan"].get("downspeed") and not result["wan"].get("upspeed"):
            try:
                sys_data = await self._api_get(API_SYSTEM_STATUS)
                wan_stats = sys_data.get("wanStatistics", sys_data.get("wan", {}))
                if isinstance(wan_stats, dict):
                    result["wan"] = {
                        "downspeed": int(wan_stats.get("downspeed", 0)),
                        "upspeed": int(wan_stats.get("upspeed", 0)),
                        "download": int(wan_stats.get("download", 0)),
                        "upload": int(wan_stats.get("upload", 0)),
                    }
                if not result["count"].get("online"):
                    sys_count = sys_data.get("count", 0)
                    if isinstance(sys_count, (int, str)):
                        result["count"] = {
                            "online": int(sys_count),
                            "all": int(sys_count),
                        }
            except (MiWiFiConnectionError, MiWiFiAuthError) as err:
                _LOGGER.debug("Could not fetch system status: %s", err)

        # Extract hardware info if present
        hardware = data.get("hardware", {})
        if isinstance(hardware, dict) and hardware:
            result["hardware"] = {
                "platform": hardware.get("platform", ""),
                "version": hardware.get("version", ""),
                "displayName": hardware.get("displayName", ""),
            }

        return result

    async def get_device_list(self) -> dict[str, Any]:
        """Get detailed device list with more per-device information."""
        data = await self._api_get(API_DEVICE_LIST)

        result: dict[str, Any] = {
            "dev": [],
            "count": {},
        }

        raw_devs = data.get("list", data.get("dev", []))
        if isinstance(raw_devs, dict):
            raw_devs = list(raw_devs.values())
            flat_devs: list[Any] = []
            for v in raw_devs:
                if isinstance(v, list):
                    flat_devs.extend(v)
                else:
                    flat_devs.append(v)
            raw_devs = flat_devs

        devices = []
        for d in raw_devs:
            if not isinstance(d, dict):
                continue
            device = {
                "mac": d.get("mac", "").upper(),
                "name": d.get(
                    "devname",
                    d.get("name", d.get("hostname", d.get("mac", ""))),
                ),
                "online": int(d.get("online", 0)) if d.get("online") else 0,
                "upspeed": int(d.get("upspeed", 0)) if d.get("upspeed") else 0,
                "downspeed": int(d.get("downspeed", 0)) if d.get("downspeed") else 0,
                "upload": int(d.get("upload", 0)) if d.get("upload") else 0,
                "download": int(d.get("download", 0)) if d.get("download") else 0,
                "ip": d.get("ip", ""),
                "authority": d.get("authority", ""),
                "isap": int(d.get("isap", 0)) if d.get("isap") else 0,
                "oui": d.get("oui", ""),
                "push": int(d.get("push", 0)) if d.get("push") else 0,
                "router": d.get("router", ""),
                "channel": d.get("channel", ""),
                "signal": int(d.get("signal", 0)) if d.get("signal") else 0,
            }
            devices.append(device)
        result["dev"] = devices

        count = data.get("count", {})
        if isinstance(count, dict):
            result["count"] = {
                "online": int(count.get("online", 0)),
                "all": int(count.get("all", 0)),
            }

        if data.get("mac"):
            result["router_mac"] = data["mac"]

        return result

    async def get_init_info(self) -> dict[str, Any]:
        """Get router hardware/firmware info (cached for 5 minutes)."""
        if self._init_info_cache and time.time() < self._init_info_expire:
            return self._init_info_cache

        data = await self._api_get(API_INIT_INFO)

        result: dict[str, Any] = {
            "hardware": {},
            "mac": "",
        }

        hardware = data.get("hardware", {})
        if isinstance(hardware, dict):
            result["hardware"] = {
                "platform": hardware.get("platform", ""),
                "version": hardware.get("version", ""),
                "displayName": hardware.get("displayName", ""),
                "sn": hardware.get("sn", ""),
                "channel": hardware.get("channel", ""),
            }

        result["mac"] = data.get("mac", "")

        self._init_info_cache = result
        self._init_info_expire = time.time() + 300

        if isinstance(hardware, dict):
            if hardware.get("displayName"):
                self._model = hardware["displayName"]
            if hardware.get("version"):
                self._firmware = hardware["version"]

        return result

    async def get_newstatus(self) -> dict[str, Any]:
        """Get extended status with per-band device counts and hardware info."""
        data = await self._api_get(API_NEWSTATUS)

        result: dict[str, Any] = {}

        count = data.get("count", {})
        if isinstance(count, dict):
            result["count"] = {
                "online_2g": int(count.get("2g", 0)),
                "online_5g": int(count.get("5g", 0)),
                "online_5g_game": int(count.get("5g-1", 0)),
                "online_lan": int(count.get("lan", 0)),
            }
        else:
            result["count"] = {
                "online_2g": 0,
                "online_5g": 0,
                "online_5g_game": 0,
                "online_lan": 0,
            }

        hardware = data.get("hardware", {})
        if isinstance(hardware, dict):
            result["hardware"] = {
                "platform": hardware.get("platform", ""),
                "version": hardware.get("version", ""),
                "displayName": hardware.get("displayName", ""),
                "mac": hardware.get("mac", ""),
            }

        return result
    async def reboot(self) -> bool:
        """Reboot the router.

        Uses the existing stok management mechanism:
        - _ensure_stok() validates current stok, re-login if expired
        - If router returns 401/403, stok is cleared and we retry once
        - After reboot, the current stok becomes invalid; the next
          coordinator poll will detect this and re-login automatically.

        Returns True on success (router accepted the reboot command).
        Raises MiWiFiConnectionError or MiWiFiAuthError on failure.
        """
        stok = await self._ensure_stok()
        session = self._get_session()
        url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{API_REBOOT}"

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    _LOGGER.info("Stok rejected for reboot, re-authenticating")
                    self._stok = None
                    stok = await self._ensure_stok()
                    url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{API_REBOOT}"
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp2:
                        if resp2.status != 200:
                            raise MiWiFiConnectionError(
                                f"Reboot failed: status {resp2.status}"
                            )
                        data = await resp2.json(content_type=None)
                elif resp.status != 200:
                    raise MiWiFiConnectionError(
                        f"Reboot failed: status {resp.status}"
                    )
                else:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            self._stok = None
            raise MiWiFiConnectionError(f"Reboot request failed: {err}") from err

        code = data.get("code") if isinstance(data, dict) else None
        if code == 0:
            _LOGGER.info("Router reboot command accepted for %s", self._host)
            self._stok = None
            return True

        _LOGGER.error("Router reboot failed: %s", data)
        raise MiWiFiConnectionError(f"Reboot failed: {data}")


    async def test_connection(self) -> bool:
        """Test if we can connect and authenticate with the router.

        Returns True on success, raises exceptions on failure.
        After successful test, calls logout() to clean up the server-side
        session so the integration's first login won't get "not auth".
        """
        try:
            await self._login()
            return True
        finally:
            # Always logout after test to prevent session conflicts
            # when the integration starts its own login
            if self._stok:
                _LOGGER.debug(
                    "Test connection succeeded, logging out to clean session for %s",
                    self._host,
                )
                try:
                    await self.logout()
                except Exception as err:
                    _LOGGER.debug("Post-test logout failed (non-fatal): %s", err)

    @property
    def model(self) -> str:
        """Return the router model name."""
        return self._model or "MiWiFi Router"

    @property
    def firmware(self) -> str:
        """Return the router firmware version."""
        return self._firmware or "unknown"

    @property
    def mac(self) -> str:
        """Return the router MAC address."""
        return self._mac or ""

    def invalidate_stok(self) -> None:
        """Mark the current stok as invalid.

        Called by the coordinator when it detects auth errors in the data
        (e.g., code > 0 in response body), so the next API call will
        trigger a re-login.
        """
        if self._stok:
            _LOGGER.debug("Stok invalidated for %s", self._host)
            self._stok = None
