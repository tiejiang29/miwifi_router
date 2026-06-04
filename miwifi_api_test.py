#!/usr/bin/env python3
"""MiWiFi Router Full API Diagnostic.

Tests ALL API endpoints used by the integration.
NO external dependencies - Python standard library only.

Run: python miwifi_api_test.py 172.16.1.1 YOUR_PASSWORD
"""

import sys
import hashlib
import time
import random
import json
import urllib.request
import urllib.parse

PUBLIC_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def http_get(url, timeout=5):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def http_post(url, data, timeout=10):
    try:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def login(host, password):
    """Login and return stok, or None on failure."""
    base_url = f"http://{host}"
    login_url = f"{base_url}/cgi-bin/luci/api/xqsystem/login"
    nonce = f"0_00:00:00:00:00:00_{int(time.time())}_{random.randint(1000, 9999)}"
    pwd_hash = sha256(password + PUBLIC_KEY)
    login_pwd = sha256(nonce + pwd_hash)

    status, text = http_post(login_url, {
        "username": "admin",
        "password": login_pwd,
        "logtype": "2",
        "nonce": nonce,
    })

    if status == 200:
        try:
            data = json.loads(text)
            if data.get("code") == 0:
                url = data.get("url", "")
                if ";stok=" in url:
                    return url.split(";stok=")[1].split("/")[0]
        except Exception:
            pass
    return None


def test_endpoint(base_url, stok, path, name):
    """Test a single API endpoint and print result."""
    if stok:
        url = f"{base_url}/cgi-bin/luci/;stok={stok}{path}"
    else:
        url = f"{base_url}{path}"

    status, text = http_get(url)

    # Try to parse JSON
    data = None
    try:
        data = json.loads(text)
    except Exception:
        pass

    if status == 200 and data is not None:
        code = data.get("code", "N/A")
        if code == 0:
            print(f"  ✅ {name}")
            print(f"     Path: {path}")
            print(f"     Status: {status}, Code: {code}")
            # Print interesting fields
            compact = json.dumps(data, ensure_ascii=False)
            if len(compact) > 300:
                compact = compact[:300] + "..."
            print(f"     Data: {compact}")
        else:
            print(f"  ⚠️  {name}")
            print(f"     Path: {path}")
            print(f"     Status: {status}, Code: {code}, Msg: {data.get('msg', '')}")
    elif status == 200:
        print(f"  ⚠️  {name} (not JSON)")
        print(f"     Path: {path}")
        print(f"     Body: {text[:150]}")
    else:
        print(f"  ❌ {name}")
        print(f"     Path: {path}")
        print(f"     Status: {status}")
    print()


def main():
    if len(sys.argv) < 3:
        print("Usage: python miwifi_api_test.py <router_ip> <password>")
        sys.exit(1)

    host = sys.argv[1]
    password = sys.argv[2]
    base_url = f"http://{host}"

    print("=" * 60)
    print("MiWiFi Router Full API Diagnostic")
    print("=" * 60)
    print(f"Host: {host}")
    print()

    # ===== Phase 1: Login =====
    print("===== Phase 1: Login =====")
    stok = login(host, password)
    if not stok:
        print("❌ Login FAILED! Cannot test authenticated endpoints.")
        return
    print(f"✅ Login OK! Stok: {stok}")
    print()

    # ===== Phase 2: Unauthenticated endpoints =====
    print("===== Phase 2: Unauthenticated Endpoints =====")
    print()

    unauth_endpoints = [
        ("/cgi-bin/luci/api/xqsystem/init_info", "Init Info (unauth)"),
    ]
    for path, name in unauth_endpoints:
        test_endpoint(base_url, None, path, name)

    # ===== Phase 3: Authenticated endpoints (all paths we use) =====
    print("===== Phase 3: Authenticated Endpoints =====")
    print()

    # All endpoint paths we need - test BOTH /api/misystem/ and /api/xqsystem/ variants
    auth_endpoints = [
        # --- Primary endpoints (used by integration) ---
        ("/api/misystem/status", "Status (misystem)"),
        ("/api/misystem/device_list", "Device List (misystem)"),
        ("/api/misystem/newstatus", "New Status (misystem)"),
        ("/cgi-bin/luci/api/xqsystem/init_info", "Init Info (xqsystem/luci)"),
        ("/api/xqsystem/device_list", "Device List (xqsystem)"),
        ("/api/xqsystem/init_info", "Init Info (xqsystem)"),
        ("/api/xqsystem/status", "System Status (xqsystem)"),

        # --- Network endpoints ---
        ("/api/xqnetwork/wifi_detail_all", "WiFi Detail (xqnetwork)"),
        ("/api/misystem/wifi_detail_all", "WiFi Detail (misystem)"),
        ("/api/xqnetwork/wifi_list", "WiFi List (xqnetwork)"),
        ("/api/xqnetwork/wan_info", "WAN Info"),

        # --- Additional useful endpoints ---
        ("/api/misystem/topo_graph", "Topo Graph"),
        ("/api/misystem/bandwidth_test", "Bandwidth Test"),
        ("/api/xqsystem/fac_info", "Factory Info"),
        ("/api/xqsystem/upgrade", "Upgrade Info"),
        ("/api/xqsystem/reboot", "Reboot (just checking path)"),
        ("/api/misystem/leds", "LED Control"),
        ("/api/misystem/smartcontroller", "Smart Controller"),
        ("/api/misystem/qos_info", "QoS Info"),
        ("/api/xqsystem/country_code", "Country Code"),
    ]

    working = []
    failed = []
    for path, name in auth_endpoints:
        url = f"{base_url}/cgi-bin/luci/;stok={stok}{path}"
        status, text = http_get(url)
        try:
            data = json.loads(text)
            code = data.get("code", "N/A")
            if status == 200 and code == 0:
                print(f"  ✅ {name}: {path}")
                compact = json.dumps(data, ensure_ascii=False)
                if len(compact) > 200:
                    compact = compact[:200] + "..."
                print(f"     {compact}")
                working.append((path, name))
            else:
                print(f"  ❌ {name}: {path} → status={status}, code={code}")
                failed.append((path, name))
        except Exception:
            print(f"  ❌ {name}: {path} → status={status}, not JSON")
            failed.append((path, name))
        print()

    # ===== Summary =====
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print()
    print(f"Working endpoints ({len(working)}):")
    for path, name in working:
        print(f"  ✅ {name}: {path}")
    print()
    print(f"Failed endpoints ({len(failed)}):")
    for path, name in failed:
        print(f"  ❌ {name}: {path}")
    print()

    # ===== Recommend config =====
    print("=" * 60)
    print("RECOMMENDED ENDPOINT CONFIG")
    print("=" * 60)
    print()

    status_path = None
    device_list_path = None
    init_info_path = None

    for path, name in working:
        if "Status (misystem)" in name:
            status_path = path
        elif "Device List (misystem)" in name:
            device_list_path = path
        elif "Init Info (xqsystem/luci)" in name:
            init_info_path = path

    # Fallbacks
    if not status_path:
        for path, name in working:
            if "Status" in name:
                status_path = path
                break
    if not device_list_path:
        for path, name in working:
            if "Device List" in name:
                device_list_path = path
                break
    if not init_info_path:
        for path, name in working:
            if "Init Info" in name:
                init_info_path = path
                break

    print(f"  API_STATUS = \"{status_path or 'NOT FOUND'}\"")
    print(f"  API_DEVICE_LIST = \"{device_list_path or 'NOT FOUND'}\"")
    print(f"  API_INIT_INFO = \"{init_info_path or 'NOT FOUND'}\"")
    print()


if __name__ == "__main__":
    main()
