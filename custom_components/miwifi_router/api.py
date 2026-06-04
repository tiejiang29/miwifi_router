"""MiWiFi Router API Client with HTTP Keep-Alive, stok caching, and layered polling."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from typing import Any

import httpx

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


class MiWiFiAuthError(Exception):
    """Authentication error."""


class MiWiFiConnectionError(Exception):
    """Connection error."""


class MiWiFiAPIClient:
    """API client for MiWiFi router with connection pooling and stok caching."""

    def __init__(self, host: str, password: str) -> None:
        self._host = host
        self._password = password
        self._base_url = f"http://{host}"
        self._stok: str | None = None
        self._stok_expire: float = 0
        self._init_info: dict[str, Any] | None = None
        self._init_info_expire: float = 0
        # HTTP Keep-Alive: reuse TCP connections across requests
        self._client: httpx.AsyncClient | None = None
        self._mac: str | None = None
        self._model: str | None = None
        self._firmware: str | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(
                    max_keepalive_connections=2,
                    max_connections=4,
                    keepalive_expiry=30.0,
                ),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

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

    async def _login(self) -> None:
        """Authenticate with the router and cache the stok."""
        client = await self._get_client()

        # First, get init_info to extract MAC for nonce generation
        try:
            resp = await client.get(API_INIT_INFO)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    self._mac = data.get("mac", self._mac)
                    hardware = data.get("hardware", {})
                    self._model = hardware.get("displayName", "MiWiFi Router")
                    self._firmware = hardware.get("version", "unknown")
        except Exception:
            _LOGGER.debug("Could not fetch init_info during login")

        nonce = self._generate_nonce()
        password = self._build_login_password(nonce)

        try:
            resp = await client.post(
                API_LOGIN,
                data={
                    "username": "admin",
                    "password": password,
                    "logtype": "2",
                    "nonce": nonce,
                },
            )
        except httpx.HTTPError as err:
            raise MiWiFiConnectionError(f"Cannot connect to router: {err}") from err

        if resp.status_code != 200:
            raise MiWiFiConnectionError(
                f"Login HTTP error: status {resp.status_code}"
            )

        data = resp.json()
        if data.get("code") != 0:
            error_msg = data.get("msg", "Unknown error")
            if "密码错误" in str(error_msg) or data.get("code") == 401:
                raise MiWiFiAuthError(f"Invalid password: {error_msg}")
            raise MiWiFiConnectionError(f"Login failed: {error_msg}")

        # Extract stok from the URL field
        url = data.get("url", "")
        if ";stok=" in url:
            self._stok = url.split(";stok=")[1].split("/")[0]
        else:
            # Fallback: try to extract from token field
            self._stok = data.get("token", "")

        if not self._stok:
            raise MiWiFiConnectionError("Could not extract stok from login response")

        self._stok_expire = time.time() + STOK_CACHE_SECONDS
        _LOGGER.info("Successfully logged in to MiWiFi router at %s", self._host)

    async def _api_get(self, endpoint: str) -> dict[str, Any]:
        """Make an authenticated API GET request."""
        stok = await self._ensure_stok()
        client = await self._get_client()
        url = f"/cgi-bin/luci/;stok={stok}{endpoint}"

        try:
            resp = await client.get(url)
        except httpx.HTTPError as err:
            # Connection lost, invalidate stok for next attempt
            self._stok = None
            raise MiWiFiConnectionError(f"API request failed: {err}") from err

        if resp.status_code == 401 or resp.status_code == 403:
            # Stok expired, clear and retry once
            self._stok = None
            stok = await self._ensure_stok()
            url = f"/cgi-bin/luci/;stok={stok}{endpoint}"
            resp = await client.get(url)

        if resp.status_code != 200:
            raise MiWiFiConnectionError(
                f"API error: {endpoint} returned status {resp.status_code}"
            )

        data = resp.json()
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
        # Try both 'list' and direct array formats
        raw_devs = data.get("list", data.get("dev", []))
        if isinstance(raw_devs, dict):
            raw_devs = list(raw_devs.values())
            # Flatten if nested
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
        """Get router hardware/firmware info (static, poll infrequently).

        This endpoint returns information that rarely changes.
        Cache it for 5 minutes internally.
        """
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

        # Cache for 5 minutes
        self._init_info = result
        self._init_info_expire = time.time() + 300

        # Also update model info
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
        """Test if we can connect and authenticate with the router."""
        try:
            await self._login()
            return True
        except (MiWiFiAuthError, MiWiFiConnectionError):
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
