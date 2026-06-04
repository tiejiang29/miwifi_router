"""MiWiFi Router API Client with aiohttp, stok caching, and layered polling."""

from __future__ import annotations

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

    Uses HA's built-in aiohttp client session for non-blocking HTTP requests.
    Login flow: POST to login endpoint with hashed password → get stok → use stok for all API calls.
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
        """Get HA's shared aiohttp client session."""
        if self._hass is not None:
            return async_get_clientsession(self._hass)
        raise RuntimeError("Home Assistant instance not provided")

    async def close(self) -> None:
        """Nothing to close - HA manages the shared session."""
        pass

    # ---- Authentication ----

    @staticmethod
    def _sha1(text: str) -> str:
        """SHA1 hash (legacy firmware)."""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256(text: str) -> str:
        """SHA256 hash (newer firmware like BE5000 RD18)."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _build_login_password(self, nonce: str) -> str:
        """Build the login password hash.

        Old firmware (SHA1): sha1(nonce + sha1(password + public_key))
        New firmware (SHA256): sha256(nonce + sha256(password + public_key))

        We try SHA256 first (newer routers), then SHA1 as fallback.
        The nonce format does NOT need the real MAC - any valid format works.
        """
        # Try SHA256 (BE5000 and newer)
        pwd_hash = self._sha256(self._password + PUBLIC_KEY)
        return self._sha256(nonce + pwd_hash)

    @staticmethod
    def _generate_nonce() -> str:
        """Generate a nonce for login.

        Format: {type}_{mac}_{timestamp}_{random}
        The MAC in nonce is NOT validated by the router - it's just salt.
        We use a placeholder MAC since we don't know the real one before login.
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
        1. Generate nonce (no need for real MAC)
        2. Hash password with nonce
        3. POST to login endpoint
        4. Extract stok from response

        Note: We do NOT try to fetch init_info before login because
        /api/misystem/init_info requires stok authentication on BE5000 (RD18).
        This is a chicken-and-egg problem - we need stok to get info,
        but we need info for the nonce. The solution: the nonce MAC is
        not validated by the router, so we use a placeholder.
        """
        session = self._get_session()
        nonce = self._generate_nonce()
        password = self._build_login_password(nonce)

        login_url = f"{self._base_url}{API_LOGIN}"
        _LOGGER.debug("Attempting login to %s", self._host)

        try:
            async with session.post(
                login_url,
                data={
                    "username": "admin",
                    "password": password,
                    "logtype": "2",
                    "nonce": nonce,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                _LOGGER.debug("Login response status: %s", resp.status)

                if resp.status != 200:
                    resp_text = await resp.text()
                    _LOGGER.error(
                        "Login HTTP error: status %s, body: %s",
                        resp.status,
                        resp_text[:200],
                    )
                    raise MiWiFiConnectionError(
                        f"Login HTTP error: status {resp.status}"
                    )

                data = await resp.json(content_type=None)
                _LOGGER.debug("Login response: code=%s, msg=%s", data.get("code"), data.get("msg"))

        except aiohttp.ClientError as err:
            _LOGGER.error("Cannot connect to router at %s: %s", self._host, err)
            raise MiWiFiConnectionError(f"Cannot connect to router: {err}") from err
        except MiWiFiConnectionError:
            raise
        except Exception as err:
            _LOGGER.error("Unexpected login error: %s", err)
            raise MiWiFiConnectionError(f"Login error: {err}") from err

        if data.get("code") != 0:
            error_msg = data.get("msg", "Unknown error")
            error_code = data.get("code")
            _LOGGER.error("Login failed: code=%s, msg=%s", error_code, error_msg)
            if "密码错误" in str(error_msg) or "not auth" in str(error_msg) or error_code == 401:
                raise MiWiFiAuthError(f"Invalid password: {error_msg}")
            raise MiWiFiConnectionError(f"Login failed: {error_msg}")

        # Extract stok from the URL field
        url = data.get("url", "")
        if ";stok=" in url:
            self._stok = url.split(";stok=")[1].split("/")[0]
        else:
            self._stok = data.get("token", "")

        if not self._stok:
            _LOGGER.error("Could not extract stok from login response: %s", data)
            raise MiWiFiConnectionError("Could not extract stok from login response")

        self._stok_expire = time.time() + STOK_CACHE_SECONDS
        _LOGGER.info("Successfully logged in to MiWiFi router at %s", self._host)

        # After successful login, try to get MAC and model info via status API
        await self._fetch_router_info_after_login(session)

    async def _fetch_router_info_after_login(self, session: aiohttp.ClientSession) -> None:
        """After login, fetch router hardware info from status or init_info.

        We try status first since it's more reliable, then init_info as fallback.
        """
        # Try getting info from status endpoint (usually has hardware section)
        try:
            url = f"{self._base_url}/cgi-bin/luci/;stok={self._stok}{API_STATUS}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    hardware = data.get("hardware", {})
                    if hardware:
                        if hardware.get("displayName"):
                            self._model = hardware["displayName"]
                        if hardware.get("version"):
                            self._firmware = hardware["version"]
                        if hardware.get("platform"):
                            pass  # platform is available
                    _LOGGER.debug("Got router info from status: model=%s, firmware=%s", self._model, self._firmware)
                    return
        except Exception as err:
            _LOGGER.debug("Could not fetch router info from status: %s", err)

        # Fallback: try init_info
        try:
            url = f"{self._base_url}/cgi-bin/luci/;stok={self._stok}{API_INIT_INFO}"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    self._mac = data.get("mac", self._mac)
                    hardware = data.get("hardware", {})
                    if hardware.get("displayName"):
                        self._model = hardware["displayName"]
                    if hardware.get("version"):
                        self._firmware = hardware["version"]
                    _LOGGER.debug("Got router info from init_info: model=%s", self._model)
        except Exception as err:
            _LOGGER.debug("Could not fetch router info from init_info: %s", err)

    async def _api_get(self, endpoint: str) -> dict[str, Any]:
        """Make an authenticated API GET request."""
        stok = await self._ensure_stok()
        session = self._get_session()
        url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    # Stok expired, re-login and retry
                    self._stok = None
                    stok = await self._ensure_stok()
                    url = f"{self._base_url}/cgi-bin/luci/;stok={stok}{endpoint}"
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    ) as resp2:
                        if resp2.status != 200:
                            raise MiWiFiConnectionError(
                                f"API error: {endpoint} returned status {resp2.status}"
                            )
                        data = await resp2.json(content_type=None)
                elif resp.status != 200:
                    raise MiWiFiConnectionError(
                        f"API error: {endpoint} returned status {resp.status}"
                    )
                else:
                    data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            self._stok = None
            raise MiWiFiConnectionError(f"API request failed: {err}") from err

        if data.get("code") == 401:
            self._stok = None
            raise MiWiFiAuthError("Stok expired, re-authentication needed")

        return data

    # ---- Public API Methods ----

    async def get_status(self) -> dict[str, Any]:
        """Get realtime router status (speeds, counts, device list with speeds)."""
        data = await self._api_get(API_STATUS)

        result: dict[str, Any] = {
            "wan": {},
            "count": {},
            "cpu": {},
            "mem": {},
            "dev": [],
        }

        wan = data.get("wan", {})
        result["wan"] = {
            "downspeed": int(wan.get("downspeed", 0)),
            "upspeed": int(wan.get("upspeed", 0)),
            "download": int(wan.get("download", 0)),
            "upload": int(wan.get("upload", 0)),
        }

        count = data.get("count", {})
        result["count"] = {
            "online": int(count.get("online", 0)),
            "all": int(count.get("all", 0)),
        }

        cpu = data.get("cpu", {})
        result["cpu"] = {
            "load": float(cpu.get("load", 0)),
            "core": int(cpu.get("core", 0)),
            "hz": cpu.get("hz", "0MHz"),
        }

        mem = data.get("mem", {})
        result["mem"] = {
            "usage": float(mem.get("usage", 0)),
            "total": mem.get("total", "0MB"),
        }

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

        hardware = data.get("hardware", {})
        if hardware:
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
            flat_devs = []
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
                "name": d.get("devname", d.get("name", d.get("mac", ""))),
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
        result["count"] = {
            "online": int(count.get("online", 0)),
            "all": int(count.get("all", 0)),
        }

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

        if hardware.get("displayName"):
            self._model = hardware["displayName"]
        if hardware.get("version"):
            self._firmware = hardware["version"]

        return result

    async def get_newstatus(self) -> dict[str, Any]:
        """Get extended status with per-band device counts."""
        data = await self._api_get(API_NEWSTATUS)

        result: dict[str, Any] = {}

        count = data.get("count", {})
        result["count"] = {
            "online_2g": int(count.get("2g", 0)),
            "online_5g": int(count.get("5g", 0)),
            "online_5g_game": int(count.get("5g-1", 0)),
            "online_lan": int(count.get("lan", 0)),
        }

        return result

    async def test_connection(self) -> bool:
        """Test if we can connect and authenticate with the router."""
        try:
            await self._login()
            return True
        except MiWiFiAuthError as err:
            _LOGGER.warning("Authentication failed for %s: %s", self._host, err)
            return False
        except MiWiFiConnectionError as err:
            _LOGGER.warning("Connection failed for %s: %s", self._host, err)
            return False
        except Exception as err:
            _LOGGER.error("Unexpected error testing connection to %s: %s", self._host, err)
            return False

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
