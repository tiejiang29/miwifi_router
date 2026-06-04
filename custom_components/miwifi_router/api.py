"""MiWiFi Router API Client with aiohttp, stok caching, and layered polling."""

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

# Timeout for API requests
REQUEST_TIMEOUT = 15


class MiWiFiAuthError(Exception):
    """Authentication error."""


class MiWiFiConnectionError(Exception):
    """Connection error."""


class MiWiFiAPIClient:
    """API client for MiWiFi router with connection pooling and stok caching.

    Uses HA's built-in aiohttp client session for non-blocking HTTP requests.
    """

    def __init__(self, host: str, password: str, hass=None) -> None:
        self._host = host
        self._password = password
        self._base_url = f"http://{host}"
        self._hass = hass
        self._stok: str | None = None
        self._stok_expire: float = 0
        self._init_info: dict[str, Any] | None = None
        self._init_info_expire: float = 0
        self._mac: str | None = None
        self._model: str | None = None
        self._firmware: str | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Get HA's shared aiohttp client session (non-blocking)."""
        if self._hass is not None:
            return async_get_clientsession(self._hass)
        raise RuntimeError("Home Assistant instance not provided to MiWiFiAPIClient")

    async def close(self) -> None:
        """Close any resources. HA manages the shared session, so nothing to close."""
        pass

    # ---- Authentication ----

    def _generate_nonce(self) -> str:
        """Generate a nonce for login: {type}_{mac}_{time}_{random}."""
        nonce_type = 0
        mac = self._mac or "00:00:00:00:00:00"
        now = int(time.time())
        rand = random.randint(1000, 9999)
        return f"{nonce_type}_{mac}_{now}_{rand}"

    @staticmethod
    def _sha1(text: str) -> str:
        """SHA1 hash."""
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _build_login_password(self, nonce: str) -> str:
        """Build the login password hash.

        Algorithm: sha1(nonce + sha1(password + public_key))
        """
        pwd_hash = self._sha1(self._password + PUBLIC_KEY)
        return self._sha1(nonce + pwd_hash)

    async def _ensure_stok(self) -> str:
        """Ensure we have a valid stok, refreshing if needed."""
        if self._stok and time.time() < self._stok_expire:
            return self._stok

        _LOGGER.debug("Stok expired or missing, logging in to %s", self._host)
        await self._login()
        return self._stok  # type: ignore[return-value]

    async def _fetch_init_info_unauth(self) -> None:
        """Try to fetch init_info without authentication to get MAC for nonce.

        Some firmware versions allow this, some don't. We try multiple paths.
        Failure is non-fatal - we fall back to a dummy MAC.
        """
        session = self._get_session()
        # Try multiple possible paths for init_info (different firmware versions)
        paths = [
            "/api/misystem/init_info",
            "/api/xqsystem/init_info",
        ]
        for path in paths:
            url = f"{self._base_url}{path}"
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if data.get("code") == 0:
                            self._mac = data.get("mac", self._mac)
                            hardware = data.get("hardware", {})
                            if hardware.get("displayName"):
                                self._model = hardware["displayName"]
                            if hardware.get("version"):
                                self._firmware = hardware["version"]
                            _LOGGER.debug(
                                "Got init_info: mac=%s, model=%s",
                                self._mac,
                                self._model,
                            )
                            return
            except Exception as err:
                _LOGGER.debug("init_info path %s failed: %s", path, err)

        _LOGGER.debug("Could not fetch init_info, using fallback MAC")

    async def _login(self) -> None:
        """Authenticate with the router and cache the stok."""
        session = self._get_session()

        # Try to get MAC from init_info (non-fatal if fails)
        await self._fetch_init_info_unauth()

        nonce = self._generate_nonce()
        password = self._build_login_password(nonce)

        login_url = f"{self._base_url}{API_LOGIN}"
        _LOGGER.debug("Attempting login to %s", login_url)

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
                allow_redirects=False,
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
                _LOGGER.debug("Login response code: %s", data.get("code"))

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
            _LOGGER.error(
                "Login failed: code=%s, msg=%s", error_code, error_msg
            )
            if "密码错误" in str(error_msg) or error_code == 401:
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
                    # Stok expired, clear and retry once
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
        """Get realtime router status (speeds, counts, device list with speeds).

        This is the primary endpoint for high-frequency polling.
        Returns: wan speeds, cpu, mem, device counts, per-device speed data.
        """
        data = await self._api_get(API_STATUS)

        result: dict[str, Any] = {
            "wan": {},
            "count": {},
            "cpu": {},
            "mem": {},
            "dev": [],
        }

        # WAN speeds
        wan = data.get("wan", {})
        result["wan"] = {
            "downspeed": int(wan.get("downspeed", 0)),
            "upspeed": int(wan.get("upspeed", 0)),
            "download": int(wan.get("download", 0)),
            "upload": int(wan.get("upload", 0)),
        }

        # Device counts
        count = data.get("count", {})
        result["count"] = {
            "online": int(count.get("online", 0)),
            "all": int(count.get("all", 0)),
        }

        # CPU
        cpu = data.get("cpu", {})
        result["cpu"] = {
            "load": float(cpu.get("load", 0)),
            "core": int(cpu.get("core", 0)),
            "hz": cpu.get("hz", "0MHz"),
        }

        # Memory
        mem = data.get("mem", {})
        result["mem"] = {
            "usage": float(mem.get("usage", 0)),
            "total": mem.get("total", "0MB"),
        }

        # Per-device data (with speeds!)
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

        # Hardware info (from status response)
        hardware = data.get("hardware", {})
        if hardware:
            result["hardware"] = {
                "platform": hardware.get("platform", ""),
                "version": hardware.get("version", ""),
                "displayName": hardware.get("displayName", ""),
            }

        return result

    async def get_device_list(self) -> dict[str, Any]:
        """Get detailed device list with more per-device information.

        This is a secondary endpoint for medium-frequency polling.
        Returns more device details than /api/misystem/status.
        """
        data = await self._api_get(API_DEVICE_LIST)

        result: dict[str, Any] = {
            "dev": [],
            "count": {},
        }

        # Parse device list - the structure varies by firmware
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
        """Get router hardware/firmware info (static, poll infrequently)."""
        if self._init_info and time.time() < self._init_info_expire:
            return self._init_info

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

        self._init_info = result
        self._init_info_expire = time.time() + 300

        if hardware.get("displayName"):
            self._model = hardware["displayName"]
        if hardware.get("version"):
            self._firmware = hardware["version"]

        return result

    async def get_newstatus(self) -> dict[str, Any]:
        """Get extended status with per-band device counts."""
        data = await self._api_get(API_NEWSTATUS)

        result: dict[str, Any] = {
            "count": {},
            "band": {},
        }

        count = data.get("count", {})
        result["count"] = {
            "online_2g": int(count.get("2g", 0)),
            "online_5g": int(count.get("5g", 0)),
            "online_5g_game": int(count.get("5g-1", 0)),
            "online_lan": int(count.get("lan", 0)),
        }

        return result

    async def test_connection(self) -> bool:
        """Test if we can connect and authenticate with the router.

        Returns True on success, False on failure. Logs errors for debugging.
        """
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
