#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# --- Configuration ---
UDM_IP = os.getenv("UDM_IP")
API_KEY = os.getenv("UNIFI_API_KEY")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
VLAN_ID = int(os.getenv("VLAN_ID", "0")) or None

# UniFi usergroup _id for the throttled WiFi Speed Limit profile.
# Find it by running: uv run internet_blocker.py --dump <mac>
# and reading the "usergroup_id" field after applying the profile in the UI.
THROTTLE_PROFILE_ID = os.getenv("THROTTLE_PROFILE_ID", "")

# Minimum rx_rate in bytes/sec to count as active use (default: 50 Kbps = 6250 bytes/sec).
# Increase this if idle background sync is triggering false counts.
ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC = 6_250

STATE_FILE = Path(__file__).parent / "data" / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_clients() -> dict[str, dict]:
    """
    Parse CLIENT* entries from .env. Format per line:
        CLIENT1=name,mac,60min
        CLIENT2=name,mac,unlimited
    """
    clients = {}
    for key in sorted(k for k in os.environ if k.startswith("CLIENT")):
        raw = os.environ[key]
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            log.warning("Skipping malformed %s (expected name,mac,limit): %s", key, raw)
            continue
        name, mac, quota = parts
        if quota.lower() == "unlimited":
            limit_seconds = None
        else:
            try:
                limit_seconds = int(quota.lower().replace("min", "")) * 60
            except ValueError:
                log.warning("Skipping %s — invalid limit %r (use e.g. 60min or unlimited)", key, quota)
                continue
        clients[name] = {"mac": mac.lower(), "limit_seconds": limit_seconds}
    return clients


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def load_state(clients: dict) -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            for key, entry in state.items():
                if key == "last_reset_date":
                    continue
                if "minutes" in entry and "seconds" not in entry:
                    entry["seconds"] = entry.pop("minutes") * 60
                if "blocked" in entry and "throttled" not in entry:
                    entry["throttled"] = entry.pop("blocked")
            return state
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt or unreadable — starting fresh.")
    return {name: {"seconds": 0, "throttled": False} for name in clients}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def fetch_active_clients() -> dict[str, dict] | None:
    """Returns mac -> client data dict, or None on API error."""
    url = f"https://{UDM_IP}/proxy/network/api/s/default/stat/sta"
    try:
        resp = requests.get(url, headers=_headers(), verify=False, timeout=10)
        resp.raise_for_status()
        clients = [c for c in resp.json().get("data", []) if "mac" in c]
        if VLAN_ID is not None:
            clients = [c for c in clients if c.get("vlan") == VLAN_ID]
        return {c["mac"].lower(): c for c in clients}
    except requests.RequestException as e:
        log.error("Failed to fetch active clients: %s", e)
        return None


def _stamgr(cmd: str, mac: str) -> bool:
    url = f"https://{UDM_IP}/proxy/network/api/s/default/cmd/stamgr"
    try:
        resp = requests.post(url, headers=_headers(), json={"cmd": cmd, "mac": mac},
                             verify=False, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("stamgr %s %s failed: %s", cmd, mac, e)
        return False


def _update_user(mac: str, updates: dict) -> bool:
    """Look up a client by MAC in /rest/user and apply updates via PUT."""
    list_url = f"https://{UDM_IP}/proxy/network/api/s/default/rest/user"
    try:
        resp = requests.get(list_url, headers=_headers(), verify=False, timeout=10)
        resp.raise_for_status()
        user = next((u for u in resp.json().get("data", [])
                     if u.get("mac", "").lower() == mac), None)
        if not user:
            log.error("No user record found for MAC %s.", mac)
            return False
        user.update(updates)
        put_url = f"{list_url}/{user['_id']}"
        resp = requests.put(put_url, headers=_headers(), json=user, verify=False, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to update user %s: %s", mac, e)
        return False


def throttle_client(mac: str) -> bool:
    return _update_user(mac, {"usergroup_id": THROTTLE_PROFILE_ID})


def unthrottle_client(mac: str) -> bool:
    return _update_user(mac, {"usergroup_id": ""})


def notify(name: str, seconds_used: int, limit_seconds: int) -> None:
    """Hook for future notifications (e.g. WhatsApp via Twilio or similar)."""
    pass


def run_reset(clients: dict) -> None:
    log.info("Running daily reset — unblocking and removing rate limits for all clients.")
    for name, cfg in clients.items():
        mac = cfg["mac"]
        _stamgr("unblock-sta", mac)  # clears any hard block from previous script versions
        if unthrottle_client(mac):
            log.info("Unthrottled %s (%s).", name, mac)
    fresh = {name: {"seconds": 0, "throttled": False} for name in clients}
    fresh["last_reset_date"] = date.today().isoformat()
    save_state(fresh)
    log.info("Reset complete.")


def run_monitor(clients: dict) -> None:
    state = load_state(clients)
    active_network_clients = fetch_active_clients()

    if active_network_clients is None:
        log.warning("API error — skipping this poll.")
        return

    vlan_str = f"VLAN{VLAN_ID}" if VLAN_ID else "all VLANs"
    log.info("--- Poll: %d device(s) on %s ---", len(active_network_clients), vlan_str)

    for name, cfg in clients.items():
        mac = cfg["mac"]
        limit_seconds = cfg["limit_seconds"]
        person = state.setdefault(name, {"seconds": 0, "throttled": False})

        if person["throttled"]:
            log.info("%s already throttled, skipping.", name)
            continue

        client = active_network_clients.get(mac)
        if client is None:
            log.info("%s not on network.", name)
            continue

        rx_rate = client.get("rx_rate") or 0
        if rx_rate < ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC:
            log.info("%s idle (rx_rate=%d bytes/s), not counting.", name, rx_rate)
            continue

        person["seconds"] += POLL_INTERVAL_SECONDS
        minutes_used = person["seconds"] // 60

        if limit_seconds is None:
            log.info("%s active — %dm used (unlimited).", name, minutes_used)
        else:
            limit_minutes = limit_seconds // 60
            log.info("%s active — %dm/%dm used.", name, minutes_used, limit_minutes)
            notify(name, person["seconds"], limit_seconds)
            if person["seconds"] >= limit_seconds:
                log.info("Limit reached — applying throttle profile to %s.", name)
                if throttle_client(mac):
                    person["throttled"] = True

    save_state(state)


def main() -> None:
    if not UDM_IP or not API_KEY:
        log.error("UDM_IP and UNIFI_API_KEY must be set in .env")
        sys.exit(1)

    clients = load_clients()
    if not clients:
        log.error("No CLIENT* entries found in .env. See .env.example for format.")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Remove rate limits and reset counters for all clients, then exit")
    parser.add_argument("--dump", metavar="MAC",
                        help="Print the full API user record for a MAC address, then exit")
    args = parser.parse_args()

    if args.reset:
        run_reset(clients)
        return

    if args.dump:
        mac = args.dump.lower()
        url = f"https://{UDM_IP}/proxy/network/api/s/default/rest/user"
        resp = requests.get(url, headers=_headers(), verify=False, timeout=10)
        resp.raise_for_status()
        user = next((u for u in resp.json().get("data", [])
                     if u.get("mac", "").lower() == mac), None)
        if not user:
            print(f"No user record found for {mac}")
        else:
            print(json.dumps(user, indent=2))
        return

    log.info("Starting internet blocker.")
    for name, cfg in clients.items():
        limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "unlimited"
        log.info("  %s — %s — limit: %s", name, cfg["mac"], limit_str)
    vlan_str = f"VLAN{VLAN_ID}" if VLAN_ID else "all VLANs"
    log.info("Polling %s every %ds | Throttle profile: %s | Active threshold: %d bytes/s",
             vlan_str, POLL_INTERVAL_SECONDS, THROTTLE_PROFILE_ID or "NOT SET",
             ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC)

    while True:
        state = load_state(clients)
        if state.get("last_reset_date") != date.today().isoformat():
            run_reset(clients)

        run_monitor(clients)
        log.info("Sleeping %ds until next poll.", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
