#!/usr/bin/env python3
"""MiWiFi Router login diagnostic script.

Run this on your Home Assistant server to debug login issues:
  python3 miwifi_diag.py 172.16.1.1 YOUR_PASSWORD

It will try all possible algorithm combinations and show detailed output.
"""

import sys
import hashlib
import time
import random
import json
import aiohttp
import asyncio

PUBLIC_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def diagnose(host, password):
    base_url = f"http://{host}"
    print(f"=" * 60)
    print(f"MiWiFi Router Login Diagnostic")
    print(f"=" * 60)
    print(f"Host: {host}")
    print(f"Password: {'*' * len(password)}")
    print(f"Public Key: {PUBLIC_KEY}")
    print()

    async with aiohttp.ClientSession() as session:

        # Step 1: Try to get init_info (various paths)
        print("--- Step 1: Try unauthenticated endpoints ---")
        test_paths = [
            "/api/misystem/init_info",
            "/api/xqsystem/init_info",
            "/cgi-bin/luci/api/xqsystem/init_info",
            "/api/misystem/topo_graph",
            "/api/xqsystem/fac_info",
        ]
        mac = None
        for path in test_paths:
            url = f"{base_url}{path}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    text = await resp.text()
                    print(f"  {path}: status={resp.status}, body={text[:150]}")
                    if resp.status == 200:
                        try:
                            data = json.loads(text)
                            if data.get("mac"):
                                mac = data["mac"]
                                print(f"    → Found MAC: {mac}")
                            if data.get("hardware", {}).get("displayName"):
                                print(f"    → Model: {data['hardware']['displayName']}")
                        except Exception:
                            pass
            except Exception as err:
                print(f"  {path}: ERROR - {err}")

        # Step 2: Try to get MAC from login page HTML
        print()
        print("--- Step 2: Check login page for MAC ---")
        try:
            async with session.get(base_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                html = await resp.text()
                # Look for MAC in meta tags or scripts
                import re
                mac_patterns = re.findall(r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', html)
                if mac_patterns:
                    print(f"  MACs found in HTML: {mac_patterns[:5]}")
                    if not mac:
                        mac = mac_patterns[0]
                else:
                    print(f"  No MAC found in HTML (page length: {len(html)})")
        except Exception as err:
            print(f"  Error fetching login page: {err}")

        print()
        print(f"  Using MAC for nonce: {mac or '00:00:00:00:00:00'}")

        # Step 3: Generate nonce
        print()
        print("--- Step 3: Generate nonce and test login ---")
        nonce_mac = mac or "00:00:00:00:00:00"
        now = int(time.time())
        rand = random.randint(1000, 9999)
        nonce = f"0_{nonce_mac}_{now}_{rand}"
        print(f"  Nonce: {nonce}")

        # Step 4: Try all algorithm combinations
        print()
        print("--- Step 4: Try all hash algorithm combinations ---")

        algorithms = [
            ("SHA256+SHA256", sha256, sha256),
            ("SHA1+SHA1", sha1, sha1),
            ("SHA256+SHA1", sha256, sha1),  # inner SHA1, outer SHA256
            ("SHA1+SHA256", sha1, sha256),  # inner SHA256, outer SHA1
        ]

        login_url = f"{base_url}/cgi-bin/luci/api/xqsystem/login"

        for algo_name, outer_hash, inner_hash in algorithms:
            pwd_hash = inner_hash(password + PUBLIC_KEY)
            login_pwd = outer_hash(nonce + pwd_hash)

            print(f"\n  Algorithm: {algo_name}")
            print(f"    inner hash: {pwd_hash}")
            print(f"    login pwd:  {login_pwd} ({len(login_pwd)} chars)")

            try:
                async with session.post(
                    login_url,
                    data={
                        "username": "admin",
                        "password": login_pwd,
                        "logtype": "2",
                        "nonce": nonce,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    text = await resp.text()
                    print(f"    HTTP status: {resp.status}")
                    print(f"    Response: {text[:200]}")
                    try:
                        data = json.loads(text)
                        if data.get("code") == 0:
                            print(f"    ✅ SUCCESS! {algo_name} works!")
                            url = data.get("url", "")
                            if ";stok=" in url:
                                stok = url.split(";stok=")[1].split("/")[0]
                                print(f"    Stok: {stok}")
                        else:
                            print(f"    ❌ Failed: code={data.get('code')}, msg={data.get('msg')}")
                    except Exception:
                        pass
            except Exception as err:
                print(f"    ❌ Request error: {err}")

        # Step 5: Also try with real MAC in nonce (if we found it)
        if mac and mac != "00:00:00:00:00:00":
            print()
            print("--- Step 5: Retry with real MAC in nonce ---")
            nonce2 = f"0_{mac}_{now}_{rand + 1}"
            print(f"  Nonce with real MAC: {nonce2}")

            for algo_name, outer_hash, inner_hash in algorithms:
                pwd_hash = inner_hash(password + PUBLIC_KEY)
                login_pwd = outer_hash(nonce2 + pwd_hash)

                try:
                    async with session.post(
                        login_url,
                        data={
                            "username": "admin",
                            "password": login_pwd,
                            "logtype": "2",
                            "nonce": nonce2,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        text = await resp.text()
                        try:
                            data = json.loads(text)
                            if data.get("code") == 0:
                                print(f"  ✅ {algo_name} + real MAC → SUCCESS!")
                            else:
                                print(f"  ❌ {algo_name} + real MAC → code={data.get('code')}, msg={data.get('msg')}")
                        except Exception:
                            print(f"  Response: {text[:100]}")
                except Exception as err:
                    print(f"  ❌ {algo_name} + real MAC → error: {err}")

    print()
    print("=" * 60)
    print("Diagnostic complete")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 miwifi_diag.py <router_ip> <password>")
        sys.exit(1)

    asyncio.run(diagnose(sys.argv[1], sys.argv[2]))
