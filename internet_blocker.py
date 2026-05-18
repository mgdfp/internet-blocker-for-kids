#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# --- Configuration ---
UDM_IP = os.getenv("UDM_IP")
API_KEY = os.getenv("UNIFI_API_KEY")

DAILY_LIMIT_MINUTES = 60
POLL_INTERVAL_MINUTES = 5

# Minimum rx_rate in bytes/sec to count as active use (default: 50 Kbps = 6250 bytes/sec).
# Increase this if idle background sync is triggering false counts.
ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC = 6_250

KIDS = {
    "alice": "aa:bb:cc:dd:ee:ff",  # replace with real MAC addresses
    "bob":   "11:22:33:44:55:66",
}

STATE_FILE = Path(__file__).parent / "data" / "state.json"

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt or unreadable — starting fresh.")
    return {name: {"minutes": 0, "blocked": False} for name in KIDS}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def fetch_active_clients() -> dict[str, dict]:
    url = f"https://{UDM_IP}/proxy/network/api/s/default/stat/sta"
    try:
        resp = requests.get(url, headers=_headers(), verify=False, timeout=10)
        resp.raise_for_status()
        return {c["mac"].lower(): c for c in resp.json().get("data", []) if "mac" in c}
    except requests.RequestException as e:
        log.error("Failed to fetch active clients: %s", e)
        return {}


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


def notify(kid_name: str, minutes_used: int, limit: int) -> None:
    """Hook for future notifications (e.g. WhatsApp via Twilio or similar)."""
    pass


def run_monitor() -> None:
    state = load_state()
    active_clients = fetch_active_clients()

    if not active_clients:
        log.warning("No active clients returned — possible API error, skipping update.")
        return

    for name, mac in KIDS.items():
        kid = state.setdefault(name, {"minutes": 0, "blocked": False})
        mac = mac.lower()

        if kid["blocked"]:
            log.info("%s already blocked, skipping.", name)
            continue

        client = active_clients.get(mac)
        if client is None:
            log.info("%s (%s) not on network.", name, mac)
            continue

        rx_rate = client.get("rx_rate") or 0
        if rx_rate < ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC:
            log.info("%s idle (rx_rate=%d bytes/s), not counting.", name, rx_rate)
            continue

        kid["minutes"] += POLL_INTERVAL_MINUTES
        log.info("%s active — %d/%d min used.", name, kid["minutes"], DAILY_LIMIT_MINUTES)
        notify(name, kid["minutes"], DAILY_LIMIT_MINUTES)

        if kid["minutes"] >= DAILY_LIMIT_MINUTES:
            log.info("Limit reached — blocking %s (%s).", name, mac)
            if _stamgr("block-sta", mac):
                kid["blocked"] = True

    save_state(state)


def run_reset() -> None:
    log.info("Running midnight reset.")
    for name, mac in KIDS.items():
        if _stamgr("unblock-sta", mac.lower()):
            log.info("Unblocked %s (%s).", name, mac)
    save_state({name: {"minutes": 0, "blocked": False} for name in KIDS})
    log.info("Reset complete.")


def main() -> None:
    if not UDM_IP or not API_KEY:
        log.error("UDM_IP and UNIFI_API_KEY must be set in .env")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Reset daily limits and unblock all devices")
    args = parser.parse_args()

    run_reset() if args.reset else run_monitor()


if __name__ == "__main__":
    main()
