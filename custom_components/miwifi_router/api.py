"""MiWiFi Router API Client with aiohttp, stok caching, and layered polling.

Key design decisions:
- Login uses a FRESH aiohttp session (not HA's shared session) to avoid stale
  cookie interference. HA's shared session may contain expired sysauth cookies
  that cause the router to reject login with "not auth".
- Before each login POST, we GET the router's root page first. This triggers
  the router to clear any stale server-side session state associated with our
  IP address. Without this step, re-login after stok expiry fails because the
  router still considers the old session active.
- All authenticated requests use HA's shared session with stok in the URL.
- BE5000 (RD18) uses SHA256+SHA256 for password hashing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_DEVICE_LIST,
    API_INIT_INFO,
    API_LOGIN,
    API_NEWSTATUS,
    API_STATUS,
    API_SYSTEM_STATUS,
    API_WIFI_DETAIL,
    PUBLIC_KEY,
    STOK_CACHE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


class MiWiFiAuthError(Exception):
    """Authentication error."""


class MiWiFiConnectionError(Exception):
    """Connection error."""


class MiWiFiAPIClient:
    """API client for MiWiFi router with stok caching.

    Uses a fresh aiohttp session for login to avoid stale cookie interference,
    and HA's shared session for all authenticated API calls.
    """

    def __init__(self, host: str, password: str, hass=None) -> None:
        self._host = host
        self._password = password
        self._base_url = f"http://{host}"
        self._hass = hass
        self._stok: str | None = None
        self._stok_expire: float = 0
        self._init_info_cache: dict[str, Any] | None = None
        self._init_info_expire: float = 0
        self._mac: str | None = None
        self._model: str | None = None
        self._firmware: str | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Get HA's shared aiohttp client session for authenticated requests."""
        if self._hass is not None:
            return async_get_clientsession(self._hass)
        raise RuntimeError("Home Assistant instance not provided")

    async def close(self) -> None:
        """Nothing to close - HA manages the shared session."""

    # ---- Authentication ----

    @staticmethod
    def _sha256(text: str) -> str:
        """SHA256 hash (BE5000 RD18 and newer firmware)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _build_login_password(self, nonce: str) -> str:
        """Build the login password hash using SHA256+SHA256.

        Algorithm: sha256(nonce + sha256(password + public_key))
        This is confirmed working on BE5000 (RD18) firmware 1.0.53.
        """
        inner_hash = self._sha256(self._password + PUBLIC_KEY)
        return self._sha256(nonce + inner_hash)

    @staticmethod
    def _generate_nonce() -> str:
        """Generate a nonce for login.

        Format: {type}_{mac}_{timestamp}_{random}
        The MAC in nonce is NOT validated by the router - placeholder works.
        """
        nonce_type = 0
        placeholder_mac = "00:00:00:00:00:00"
        now = int(time.time())
        rand = random.randint(1000, 9999)
        return f"{nonce_type}_{placeholder_mac}_{now}_{rand}"

    async def _ensure_stok(self) -> str:
        """Ensure we have a valid stok, re-login if expired."""
        if self._stok and time.time() < self._stok_expire:
            return self._stok

        _LOGGER.debug("Stok expired or missing, logging in to %s", self._host)
        await self._login()
        return self._stok  # type: ignore[return-value]

    async def _login(self) -> None:
        """Authenticate with the router and cache the stok.

        Login flow:
        1. Create a fresh aiohttp session (no stale cookies)
        2. GET the router's root page first - this triggers the router to
           clear any stale server-side session associated with our IP.
           Without this, re-login fails because the router still considers
           the old session active and returns "not auth".
        3. POST the login data with SHA256+SHA256 hashed password
        4. Extract stok from response
        """
        nonce = self._generate_nonce()
        password = self._build_login_password(nonce)
        login_url = f"{self._base_url}{API_LOGIN}"

        _LOGGER.debug(
            "Attempting login to %s | nonce=%s | pwd_hash_len=%d",
            self._host, nonce, len(password),
        )

        # Use a fresh session for login to avoid stale cookie interference
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            connector=aiohttp.TCPConnector(force_close=True),
        ) as login_session:
            # Step 1: GET root page to clear stale server-side session
            # This is critical for re-login after stok expiry.
            # The router associates sessions with client IP, and without
            # this step, it rejects new login attempts with "not auth".
            try:
                async with login_session.get(
                    self._base_url,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=True,
                ) as pre_resp:
                    _LOGGER.debug(
                        "Pre-login GET status: %s (clearing stale session)",
                        pre_resp.status,
                    )
            except Exception as pre_err:
                _LOGGER.debug("Pre-login GET failed (non-fatal): %s", pre_err)

            # Step 2: POST login data
            try:
                async with login_session.post(
                    login_url,
                    data={
                        "username": "admin",
                        "password": password,
                        "logtype": "2",
                        "nonce": nonce,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as resp:
                    _LOGGER.debug("Login response status: %s", resp.status)

                    if resp.status != 200:
                        resp_text = await resp.text()
                        _LOGGER.error(
                            "Login HTTP error: status %s, body: %s",
                            resp.status,
                            resp_text[:300],
                        )
                        raise MiWiFiConnectionError(
                            f"Login HTTP error: status {resp.status}"
                        )

                    data = await resp.json(content_type=None)
                    _LOGGER.debug(
                        "Login response: code=%s, msg=%s, has_url=%s, has_token=%s",
                        data.get("code"),
                        data.get("msg"),
                        "url" in data,
                        "token" in data,
                    )

            except aiohttp.ClientError as err:
                _LOGGER.error("Cannot connect to router at %s: %s", self._host, err)
                raise MiWiFiConnectionError(
                    f"Cannot connect to router: {err}"
                ) from err
            except MiWiFiConnectionError:
                raise
            except Exception as err:
                _LOGGER.error("Unexpected login error: %s", err)
                raise MiWiFiConnectionError(f"Login error: {err}") from err

        if data.get("code") != 0:
            error_msg = data.get("msg", "Unknown error")
            error_code = data.get("code")
            _LOGGER.error(
                "Login failed for %s: code=%s, msg=%s",
                self._host, error_code, error_msg,
            )
            if (
                "密码错误" in str(error_msg)
                or "not auth" in str(error_msg)
                or error_code == 401
            ):
                raise MiWiFiAuthError(f"Invalid password: {error_msg}")
            raise MiWiFiConnectionError(f"Login failed: {error_msg}")

        # Extract stok from the URL field or token field
        url = data.get("url", "")
        if ";stok=" in url:
            self._stok = url.split(";stok=")[1].split("/")[0]
        else:
            self._stok = data.get("token", "")

        if not self._stok:
            _LOGGER.error("Could not extract stok from login response: %s", data)
            raise MiWiFiConnectionError("Could not extract stok from login response")

        self._stok_expire = time.time() + STOK_CACHE_SECONDS
        _LOGGER.info(
            "Successfully logged in to MiWiFi router at %s (stok expires in %ds)",
            self._host, STOK_CACHE_SECONDS,
        )

        # After successful login, try to get router info
        await self._fetch_router_info_after_login()

    async def _fetch_router_info_after_login(self) -> None:
        """After login, fetch router hardware info from available endpoints.

        Tries newstatus first (has hardware section with model/firmware),
        then init_info as fallback.
        Uses HA's shared session with stok in URL.
        """
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
        """Make an authenticated API GET request using stok in URL."""
        stok = await self._ensure_stok()
        session = self._get_session()
        url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"

        _LOGGER.debug("API GET: %s", endpoint)

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    # Stok expired, re-login and retry
                    _LOGGER.debug("Got %s, re-authenticating", resp.status)
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

        # Check for auth errors in response body
        if isinstance(data, dict):
            code = data.get("code")
            if code == 401:
                self._stok = None
                raise MiWiFiAuthError("Stok expired, re-authentication needed")

        return data

    # ---- Public API Methods ----

    async def get_status(self) -> dict[str, Any]:
        """Get realtime router status (speeds, counts, device list with speeds).

        Combines data from /api/misystem/status and /api/xqsystem/status
        to get a complete picture on BE5000 (RD18).
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
            # Some firmware versions return count as a number
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
                # Also get count from system status if missing
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
        """Get detailed device list with more per-device information.

        Uses /api/xqsystem/device_list which provides detailed device info
        including hostname, signal, channel, OUI, etc.
        """
        data = await self._api_get(API_DEVICE_LIST)

        result: dict[str, Any] = {
            "dev": [],
            "count": {},
        }

        # Device list from xqsystem has structure: {"mac": "...", "list": [...]}
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

        # Count from device_list response
        count = data.get("count", {})
        if isinstance(count, dict):
            result["count"] = {
                "online": int(count.get("online", 0)),
                "all": int(count.get("all", 0)),
            }

        # Also store the router's own MAC from the response
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

    async def test_connection(self) -> bool:
        """Test if we can connect and authenticate with the router.

        Returns True on success, raises exceptions on failure.
        This allows the config flow to distinguish between auth and connection errors.
        """
        await self._login()
        return True

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
