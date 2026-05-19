#!/usr/bin/env python3
import argparse
import json
import logging
import os
import random
import re
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

# Name of the VLAN4 WiFi network in UniFi (used to update the MAC allowlist).
WLAN_NAME = os.getenv("WLAN_NAME", "")

# Twilio credentials for SMS notifications to kids.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# Telegram bot for admin commands from Morgan.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # Morgan's personal chat ID

# Minimum rx_rate in bytes/sec to count as active use (default: 50 Kbps = 6250 bytes/sec).
# Increase this if idle background sync is triggering false counts.
ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC = 6_250

STATE_FILE = Path(__file__).parent / "data" / "state.json"
MESSAGES_FILE = Path(__file__).parent / "messages.json"
DYNAMIC_CLIENTS_FILE = Path(__file__).parent / "data" / "dynamic_clients.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_telegram_offset = 0
_conversation: dict = {"step": None}  # tracks multi-step /add flow per session


# ---------------------------------------------------------------------------
# Client loading
# ---------------------------------------------------------------------------

def load_clients() -> dict[str, dict]:
    """
    Parse CLIENT* entries from .env. Format per line:
        CLIENT1=name,mac,60min,+4712345678
        CLIENT2=name,mac,unlimited
    Phone number is optional — omit for clients who should not receive SMS.
    """
    clients = {}
    for key in sorted(k for k in os.environ if k.startswith("CLIENT")):
        raw = os.environ[key]
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) not in (3, 4):
            log.warning("Skipping malformed %s (expected name,mac,limit[,phone]): %s", key, raw)
            continue
        name, mac, quota = parts[0], parts[1], parts[2]
        phone = parts[3] if len(parts) == 4 else None
        if quota.lower() == "unlimited":
            limit_seconds = None
        else:
            try:
                limit_seconds = int(quota.lower().replace("min", "")) * 60
            except ValueError:
                log.warning("Skipping %s — invalid limit %r (use e.g. 60min or unlimited)", key, quota)
                continue
        clients[name] = {"mac": mac.lower(), "limit_seconds": limit_seconds, "phone": phone}
    return clients


def load_dynamic_clients() -> dict:
    """Load clients added at runtime via Telegram /add command."""
    if DYNAMIC_CLIENTS_FILE.exists():
        try:
            with open(DYNAMIC_CLIENTS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read dynamic_clients.json — starting with empty dynamic list.")
    return {}


def save_dynamic_clients(dynamic: dict) -> None:
    tmp = DYNAMIC_CLIENTS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(dynamic, f, indent=2)
    os.replace(tmp, DYNAMIC_CLIENTS_FILE)


def load_messages() -> dict:
    try:
        with open(MESSAGES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Could not load messages.json: %s — SMS will use fallback text.", e)
        return {}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(clients: dict) -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            for key, entry in state.items():
                if key in ("last_reset_date", "unknown_devices"):
                    continue
                if "minutes" in entry and "seconds" not in entry:
                    entry["seconds"] = entry.pop("minutes") * 60
                if "blocked" in entry and "throttled" not in entry:
                    entry["throttled"] = entry.pop("blocked")
            return state
        except (json.JSONDecodeError, OSError):
            log.warning("State file corrupt or unreadable — starting fresh.")
    return {name: {"seconds": 0, "throttled": False, "notified_half": False} for name in clients}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# UniFi API
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {"X-API-Key": API_KEY}


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
        if resp.status_code == 400:
            log.debug("stamgr %s %s: device not connected, skipping.", cmd, mac)
            return True
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
    ok = _update_user(mac, {"usergroup_id": THROTTLE_PROFILE_ID})
    if ok:
        time.sleep(2)
        _stamgr("kick-sta", mac)
    return ok


def unthrottle_client(mac: str) -> bool:
    ok = _update_user(mac, {"usergroup_id": ""})
    if ok:
        time.sleep(2)
        _stamgr("kick-sta", mac)
    return ok


def add_to_wlan_allowlist(mac: str) -> bool:
    if not WLAN_NAME:
        log.warning("WLAN_NAME not set — skipping allowlist update.")
        return False
    url = f"https://{UDM_IP}/proxy/network/api/s/default/rest/wlanconf"
    try:
        resp = requests.get(url, headers=_headers(), verify=False, timeout=10)
        resp.raise_for_status()
        wlan = next((w for w in resp.json().get("data", [])
                     if w.get("name") == WLAN_NAME), None)
        if not wlan:
            log.error("WLAN %r not found in UniFi.", WLAN_NAME)
            return False
        mac_list = wlan.get("mac_filter_list", [])
        if mac not in mac_list:
            mac_list.append(mac)
            wlan["mac_filter_list"] = mac_list
            # Note: intentionally not touching mac_filter_enabled —
            # we maintain the list without enforcing it.
            put_resp = requests.put(f"{url}/{wlan['_id']}", headers=_headers(),
                                    json=wlan, verify=False, timeout=10)
            put_resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Failed to update WLAN allowlist: %s", e)
        return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_sms(to_number: str, body: str) -> None:
    if not all([to_number, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        return
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": to_number, "Body": body},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("SMS sent to %s.", to_number)
    except requests.RequestException as e:
        log.error("Failed to send SMS to %s: %s", to_number, e)


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        log.error("Failed to send Telegram message: %s", e)


def _pick_message(messages: dict, event: str, **kwargs) -> str:
    options = messages.get(event, [])
    if not options:
        return ""
    return random.choice(options).format(**kwargs)


# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------

def _parse_limit(quota: str) -> int | None:
    """Parse '60min' -> 3600, 'unlimited' -> None. Raises ValueError on bad input."""
    if quota.lower() == "unlimited":
        return None
    return int(quota.lower().replace("min", "")) * 60


def _valid_mac(mac: str) -> bool:
    return bool(re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac.lower()))


def _do_add(name: str, mac: str, limit_seconds: int | None, phone: str | None,
            clients: dict) -> None:
    """Persist and activate a new client. Removes from unknown devices if present."""
    new_client = {"mac": mac, "limit_seconds": limit_seconds, "phone": phone}
    clients[name] = new_client
    dynamic = load_dynamic_clients()
    dynamic[name] = new_client
    save_dynamic_clients(dynamic)

    # If this MAC was in the unknown devices list, remove it and unthrottle —
    # it'll be tracked properly with its quota from now on.
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            if mac in state.get("unknown_devices", {}):
                del state["unknown_devices"][mac]
                unthrottle_client(mac)
                save_state(state)
        except (json.JSONDecodeError, OSError):
            pass

    add_to_wlan_allowlist(mac)

    limit_str = f"{limit_seconds // 60}min" if limit_seconds else "ubegrenset"
    sms_str = f"SMS til {phone}" if phone else "ingen SMS"
    send_telegram(
        f"✅ {name} er lagt til!\n"
        f"• MAC: {mac}\n"
        f"• Grense: {limit_str}\n"
        f"• {sms_str}"
    )
    log.info("Added %s (%s) via Telegram.", name, mac)


def handle_telegram_command(text: str, clients: dict) -> None:
    global _conversation
    text = text.strip()
    cmd = text.lower().split()[0] if text else ""

    # Cancel works at any point
    if cmd == "/cancel":
        _conversation = {"step": None}
        send_telegram("❌ Avbrutt.")
        return

    step = _conversation.get("step")

    # --- No active conversation: expect a command ---
    if step is None:
        if cmd == "/add":
            _conversation = {"step": "awaiting_name"}
            send_telegram("Hva skal personen hete? (f.eks. dag_kone, ingen mellomrom)")
        elif cmd == "/list":
            lines = []
            if clients:
                dynamic_names = load_dynamic_clients().keys()
                lines.append("📋 Kjente klienter:\n")
                for name, cfg in clients.items():
                    limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "ubegrenset"
                    source = "(lagt til)" if name in dynamic_names else "(.env)"
                    lines.append(f"• {name} — {cfg['mac']} — {limit_str} {source}")
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE) as f:
                        state = json.load(f)
                    unknowns = state.get("unknown_devices", {})
                    if unknowns:
                        lines.append("\n⚠️ Ukjente enheter (begrenset hastighet):\n")
                        for mac, info in unknowns.items():
                            lines.append(f"• {info.get('hostname', 'Ukjent')} — {mac}")
                except (json.JSONDecodeError, OSError):
                    pass
            if not lines:
                send_telegram("Ingen klienter konfigurert.")
                return
            send_telegram("\n".join(lines))
        else:
            send_telegram("Tilgjengelige kommandoer:\n/add — legg til ny person\n/list — vis alle")
        return

    # --- Active conversation steps ---
    if step == "awaiting_name":
        if not text or " " in text:
            send_telegram("❌ Navn kan ikke inneholde mellomrom. Prøv igjen:")
            return
        _conversation["name"] = text
        _conversation["step"] = "awaiting_mac"
        send_telegram(f"MAC-adresse til {text}?\n(format: aa:bb:cc:dd:ee:ff)")

    elif step == "awaiting_mac":
        if not _valid_mac(text):
            send_telegram("❌ Ugyldig MAC-adresse. Format: aa:bb:cc:dd:ee:ff\nPrøv igjen:")
            return
        _conversation["mac"] = text.lower()
        _conversation["step"] = "awaiting_limit"
        send_telegram("Tidsgrense per dag?\n• 60min\n• unlimited")

    elif step == "awaiting_limit":
        try:
            _conversation["limit_seconds"] = _parse_limit(text)
        except ValueError:
            send_telegram("❌ Skriv f.eks. 60min eller unlimited:")
            return
        _conversation["step"] = "awaiting_phone"
        send_telegram("Telefonnummer for SMS-varsler?\n(format: +4712345678, eller 'skip' for ingen SMS)")

    elif step == "awaiting_phone":
        phone = None if text.lower() == "skip" else text
        _do_add(
            name=_conversation["name"],
            mac=_conversation["mac"],
            limit_seconds=_conversation["limit_seconds"],
            phone=phone,
            clients=clients,
        )
        _conversation = {"step": None}


def check_telegram(clients: dict) -> None:
    global _telegram_offset
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        long_poll = max(5, POLL_INTERVAL_SECONDS - 5)
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": _telegram_offset, "timeout": long_poll},
            timeout=long_poll + 10,
        )
        resp.raise_for_status()
        for update in resp.json().get("result", []):
            _telegram_offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != TELEGRAM_CHAT_ID:
                log.warning("Ignoring Telegram message from unknown chat ID %s.", chat_id)
                continue
            text = msg.get("text", "").strip()
            if text:
                handle_telegram_command(text, clients)
    except requests.RequestException as e:
        log.error("Telegram poll failed: %s", e)


# ---------------------------------------------------------------------------
# Monitor & reset
# ---------------------------------------------------------------------------

def run_reset(clients: dict) -> None:
    log.info("Running daily reset — unblocking and removing rate limits for all clients.")
    state = load_state(clients)
    for name, cfg in clients.items():
        mac = cfg["mac"]
        _stamgr("unblock-sta", mac)
        if unthrottle_client(mac):
            log.info("Unthrottled %s (%s).", name, mac)
    fresh = {name: {"seconds": 0, "throttled": False, "notified_half": False} for name in clients}
    fresh["last_reset_date"] = date.today().isoformat()
    fresh["unknown_devices"] = state.get("unknown_devices", {})  # unknown stay throttled
    save_state(fresh)
    log.info("Reset complete.")


def handle_unknown_devices(active_network_clients: dict, clients: dict, state: dict) -> None:
    known_macs = {cfg["mac"].lower() for cfg in clients.values()}
    unknown_devices = state.setdefault("unknown_devices", {})

    for mac, client in active_network_clients.items():
        if mac in known_macs:
            continue
        if mac not in unknown_devices:
            hostname = client.get("hostname") or client.get("name") or mac
            log.info("Unknown device detected: %s (%s) — throttling.", hostname, mac)
            throttle_client(mac)
            unknown_devices[mac] = {"hostname": hostname, "first_seen": date.today().isoformat()}
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                msg = (f"Unknown device on network:\n"
                       f"  MAC: `{mac}`\n"
                       f"  Name: {hostname}\n\n"
                       f"Use /add to register it.")
                send_telegram(msg)
        else:
            throttle_client(mac)


def run_monitor(clients: dict, messages: dict) -> None:
    state = load_state(clients)
    active_network_clients = fetch_active_clients()

    if active_network_clients is None:
        log.warning("API error — skipping this poll.")
        return

    vlan_str = f"VLAN{VLAN_ID}" if VLAN_ID else "all VLANs"
    log.info("--- Poll: %d device(s) on %s ---", len(active_network_clients), vlan_str)

    handle_unknown_devices(active_network_clients, clients, state)

    for name, cfg in clients.items():
        mac = cfg["mac"]
        limit_seconds = cfg["limit_seconds"]
        phone = cfg["phone"]
        person = state.setdefault(name, {"seconds": 0, "throttled": False, "notified_half": False})

        if person["throttled"]:
            log.info("%s already throttled, skipping.", name)
            continue

        client = active_network_clients.get(mac)
        if client is None:
            log.info("%s not on network.", name)
            person.pop("tx_bytes", None)
            continue

        tx_bytes = client.get("tx_bytes") or 0
        prev_tx_bytes = person.get("tx_bytes")
        person["tx_bytes"] = tx_bytes

        if prev_tx_bytes is None:
            log.info("%s first seen this session — waiting for next poll to measure usage.", name)
            continue

        delta_bytes = tx_bytes - prev_tx_bytes
        if delta_bytes < 0:
            # Counter reset (device reconnected) — skip this interval
            log.info("%s tx_bytes counter reset, skipping interval.", name)
            continue

        avg_rate = delta_bytes / POLL_INTERVAL_SECONDS
        if avg_rate < ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC:
            log.info("%s idle (avg %.0f bytes/s over last %ds), not counting.", name, avg_rate, POLL_INTERVAL_SECONDS)
            continue

        person["seconds"] += POLL_INTERVAL_SECONDS
        minutes_used = person["seconds"] // 60

        if limit_seconds is None:
            log.info("%s active — %dm used (unlimited) [avg dl %.0f bytes/s].", name, minutes_used, avg_rate)
            continue

        limit_minutes = limit_seconds // 60
        log.info("%s active — %dm/%dm used [avg dl %.0f bytes/s].", name, minutes_used, limit_minutes, avg_rate)

        # 50% notification
        if not person.get("notified_half") and person["seconds"] >= limit_seconds // 2:
            remaining = (limit_seconds - person["seconds"]) // 60
            msg = _pick_message(messages, "half_quota",
                                name=name.capitalize(), used=minutes_used,
                                remaining=remaining, limit=limit_minutes)
            if msg:
                send_sms(phone, msg)
                log.info("50%% notification sent to %s.", name)
            person["notified_half"] = True

        # Throttle
        if person["seconds"] >= limit_seconds:
            log.info("Limit reached — applying throttle profile to %s.", name)
            if throttle_client(mac):
                person["throttled"] = True
                msg = _pick_message(messages, "throttled",
                                    name=name.capitalize(), used=minutes_used,
                                    limit=limit_minutes)
                if msg:
                    send_sms(phone, msg)
                    log.info("Throttle notification sent to %s.", name)

    save_state(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not UDM_IP or not API_KEY:
        log.error("UDM_IP and UNIFI_API_KEY must be set in .env")
        sys.exit(1)

    clients = load_clients()
    clients.update(load_dynamic_clients())
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

    messages = load_messages()

    log.info("Starting internet blocker.")
    for name, cfg in clients.items():
        limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "unlimited"
        sms_str = cfg["phone"] if cfg["phone"] else "no SMS"
        log.info("  %s — %s — limit: %s — %s", name, cfg["mac"], limit_str, sms_str)
    vlan_str = f"VLAN{VLAN_ID}" if VLAN_ID else "all VLANs"
    log.info("Polling %s every %ds | Throttle profile: %s | Active threshold: %d bytes/s",
             vlan_str, POLL_INTERVAL_SECONDS, THROTTLE_PROFILE_ID or "NOT SET",
             ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC)
    if TELEGRAM_BOT_TOKEN:
        log.info("Telegram bot active — listening for admin commands.")

    while True:
        check_telegram(clients)  # long-polls for up to POLL_INTERVAL_SECONDS-5s

        state = load_state(clients)
        if state.get("last_reset_date") != date.today().isoformat():
            run_reset(clients)

        run_monitor(clients, messages)


if __name__ == "__main__":
    main()
