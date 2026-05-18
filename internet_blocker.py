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

POLL_INTERVAL_SECONDS = 300  # 5 minutes

# Minimum rx_rate in bytes/sec to count as active use (default: 50 Kbps = 6250 bytes/sec).
# Increase this if idle background sync is triggering false counts.
ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC = 6_250

STATE_FILE = Path(__file__).parent / "data" / "state.json"

# --- Logging ---
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
            limit = None
        else:
            try:
                limit = int(quota.lower().replace("min", ""))
            except ValueError:
                log.warning("Skipping %s — invalid limit %r (use e.g. 60min or unlimited)", key, quota)
                continue
        clients[name] = {"mac": mac.lower(), "limit": limit}
    return clients


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def load_state(clients: dict) -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt or unreadable — starting fresh.")
    return {name: {"minutes": 0, "blocked": False} for name in clients}


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
        resp = requests.post(
            url,
            headers=_headers(),
            json={"cmd": cmd, "mac": mac},
            verify=False,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("stamgr %s %s failed: %s", cmd, mac, e)
        return False


def notify(name: str, minutes_used: int, limit: int) -> None:
    """Hook for future notifications (e.g. WhatsApp via Twilio or similar)."""
    pass


def run_reset(clients: dict) -> None:
    log.info("Running daily reset — unblocking all clients.")
    for name, cfg in clients.items():
        if _stamgr("unblock-sta", cfg["mac"]):
            log.info("Unblocked %s (%s).", name, cfg["mac"])
    fresh = {name: {"minutes": 0, "blocked": False} for name in clients}
    fresh["last_reset_date"] = date.today().isoformat()
    save_state(fresh)
    log.info("Reset complete.")


def run_monitor(clients: dict) -> None:
    state = load_state(clients)
    active_network_clients = fetch_active_clients()

    if not active_network_clients:
        log.warning("No active clients returned — possible API error, skipping update.")
        return

    log.info("--- Poll: %d device(s) on network ---", len(active_network_clients))

    poll_minutes = POLL_INTERVAL_SECONDS // 60

    for name, cfg in clients.items():
        mac = cfg["mac"]
        limit = cfg["limit"]
        person = state.setdefault(name, {"minutes": 0, "blocked": False})

        if person["blocked"]:
            log.info("%s already blocked, skipping.", name)
            continue

        client = active_network_clients.get(mac)
        if client is None:
            log.info("%s not on network.", name)
            continue

        rx_rate = client.get("rx_rate") or 0
        if rx_rate < ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC:
            log.info("%s idle (rx_rate=%d bytes/s), not counting.", name, rx_rate)
            continue

        person["minutes"] += poll_minutes

        if limit is None:
            log.info("%s active — %d min used (unlimited).", name, person["minutes"])
        else:
            log.info("%s active — %d/%d min used.", name, person["minutes"], limit)
            notify(name, person["minutes"], limit)
            if person["minutes"] >= limit:
                log.info("Limit reached — blocking %s (%s).", name, mac)
                if _stamgr("block-sta", mac):
                    person["blocked"] = True

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
                        help="Manually reset daily limits and unblock all clients, then exit")
    args = parser.parse_args()

    if args.reset:
        run_reset(clients)
        return

    log.info("Starting internet blocker.")
    for name, cfg in clients.items():
        limit_str = f"{cfg['limit']}min" if cfg["limit"] else "unlimited"
        log.info("  %s — %s — limit: %s", name, cfg["mac"], limit_str)
    log.info("Poll interval: %ds | Active threshold: %d bytes/s",
             POLL_INTERVAL_SECONDS, ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC)

    while True:
        state = load_state(clients)
        if state.get("last_reset_date") != date.today().isoformat():
            run_reset(clients)

        run_monitor(clients)
        log.info("Sleeping %ds until next poll.", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
