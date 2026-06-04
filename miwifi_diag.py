#!/usr/bin/env python3
"""MiWiFi Router login diagnostic script.

NO external dependencies - uses only Python standard library.
Run: python miwifi_diag.py 172.16.1.1 YOUR_PASSWORD
"""

import sys
import hashlib
import time
import random
import json
import urllib.request
import urllib.parse
import re

PUBLIC_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def http_get(url, timeout=5):
    """HTTP GET, return (status_code, body_text)."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def http_post(url, data, timeout=10):
    """HTTP POST with form data, return (status_code, body_text)."""
    try:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def diagnose(host, password):
    base_url = f"http://{host}"
    print(f"=" * 60)
    print(f"MiWiFi Router Login Diagnostic")
    print(f"=" * 60)
    print(f"Host: {host}")
    print(f"Password length: {len(password)} chars")
    print(f"Public Key: {PUBLIC_KEY}")
    print()

    # Step 1: Try unauthenticated endpoints to find MAC
    print("--- Step 1: Try unauthenticated endpoints ---")
    test_paths = [
        "/api/misystem/init_info",
        "/api/xqsystem/init_info",
        "/cgi-bin/luci/api/xqsystem/init_info",
        "/api/misystem/topo_graph",
        "/api/xqsystem/fac_info",
    ]
    mac = None
    model = None
    for path in test_paths:
        url = f"{base_url}{path}"
        status, text = http_get(url)
        print(f"  {path}:")
        print(f"    status={status}, body={text[:150]}")
        if status == 200:
            try:
                data = json.loads(text)
                if data.get("mac"):
                    mac = data["mac"]
                    print(f"    >>> Found MAC: {mac}")
                hw = data.get("hardware", {})
                if hw.get("displayName"):
                    model = hw["displayName"]
                    print(f"    >>> Model: {model}")
            except Exception:
                pass

    # Step 2: Try to get MAC from login page HTML
    print()
    print("--- Step 2: Check login page HTML ---")
    status, html = http_get(base_url)
    if status == 200:
        mac_patterns = re.findall(
            r'([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-]'
            r'[0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})', html)
        if mac_patterns:
            print(f"  MACs found in HTML: {mac_patterns[:5]}")
            if not mac:
                mac = mac_patterns[0]
        else:
            print(f"  No MAC found in HTML (page length: {len(html)})")
            # Look for keyMeta or deviceMac in JS
            mac_js = re.findall(r'["\']([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})["\']', html)
            if mac_js:
                print(f"  MAC from JS: {mac_js[:5]}")
                if not mac:
                    mac = mac_js[0]
    else:
        print(f"  Could not fetch login page: status={status}")

    print()
    nonce_mac = mac or "00:00:00:00:00:00"
    print(f"  Using MAC for nonce: {nonce_mac}")

    # Step 3: Generate nonce
    print()
    print("--- Step 3: Generate nonce ---")
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
        ("SHA256+SHA1", sha256, sha1),
        ("SHA1+SHA256", sha1, sha256),
    ]

    login_url = f"{base_url}/cgi-bin/luci/api/xqsystem/login"
    success_algo = None

    for algo_name, outer_hash, inner_hash in algorithms:
        pwd_hash = inner_hash(password + PUBLIC_KEY)
        login_pwd = outer_hash(nonce + pwd_hash)

        print(f"\n  Algorithm: {algo_name}")
        print(f"    inner hash (pwd+key): {pwd_hash}")
        print(f"    login pwd (nonce+inner): {login_pwd} ({len(login_pwd)} chars)")

        status, text = http_post(login_url, {
            "username": "admin",
            "password": login_pwd,
            "logtype": "2",
            "nonce": nonce,
        })
        print(f"    HTTP status: {status}")
        print(f"    Response: {text[:200]}")

        try:
            data = json.loads(text)
            if data.get("code") == 0:
                print(f"    >>> SUCCESS! {algo_name} works!")
                success_algo = algo_name
                url = data.get("url", "")
                if ";stok=" in url:
                    stok = url.split(";stok=")[1].split("/")[0]
                    print(f"    >>> Stok: {stok}")
                break
            else:
                print(f"    >>> Failed: code={data.get('code')}, msg={data.get('msg')}")
        except Exception:
            pass

    # Step 5: If all failed, try with real MAC if different
    if not success_algo and mac and mac != "00:00:00:00:00:00":
        print()
        print("--- Step 5: Retry all algorithms with real MAC ---")
        nonce2 = f"0_{mac}_{now}_{rand + 1}"
        print(f"  New nonce: {nonce2}")

        for algo_name, outer_hash, inner_hash in algorithms:
            pwd_hash = inner_hash(password + PUBLIC_KEY)
            login_pwd = outer_hash(nonce2 + pwd_hash)

            status, text = http_post(login_url, {
                "username": "admin",
                "password": login_pwd,
                "logtype": "2",
                "nonce": nonce2,
            })
            try:
                data = json.loads(text)
                if data.get("code") == 0:
                    print(f"  >>> {algo_name} + real MAC -> SUCCESS!")
                    success_algo = f"{algo_name} + real MAC"
                    break
                else:
                    print(f"  >>> {algo_name} + real MAC -> code={data.get('code')}, msg={data.get('msg')}")
            except Exception:
                print(f"  >>> {algo_name} + real MAC -> {text[:100]}")

    # Step 6: Try without public_key (direct hash)
    if not success_algo:
        print()
        print("--- Step 6: Try without public_key ---")
        nonce3 = f"0_{nonce_mac}_{now}_{rand + 2}"
        for algo_name, hash_func in [("SHA256", sha256), ("SHA1", sha1)]:
            login_pwd = hash_func(nonce3 + hash_func(password))
            print(f"\n  {algo_name}(nonce + {algo_name}(password)): {login_pwd}")
            status, text = http_post(login_url, {
                "username": "admin",
                "password": login_pwd,
                "logtype": "2",
                "nonce": nonce3,
            })
            print(f"    status={status}, response={text[:200]}")
            try:
                data = json.loads(text)
                if data.get("code") == 0:
                    print(f"    >>> SUCCESS!")
                    success_algo = f"{algo_name} no public_key"
                    break
                else:
                    print(f"    >>> Failed: code={data.get('code')}, msg={data.get('msg')}")
            except Exception:
                pass

    # Summary
    print()
    print("=" * 60)
    if success_algo:
        print(f"RESULT: Working algorithm = {success_algo}")
    else:
        print("RESULT: All algorithms failed!")
        print("Possible causes:")
        print("  1. Wrong password (check for special characters)")
        print("  2. Router uses a different public_key")
        print("  3. Router uses a completely different login algorithm")
        print("  4. Router has rate-limited login attempts")
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python miwifi_diag.py <router_ip> <password>")
        print("Example: python miwifi_diag.py 172.16.1.1 mypassword")
        sys.exit(1)

    diagnose(sys.argv[1], sys.argv[2])
