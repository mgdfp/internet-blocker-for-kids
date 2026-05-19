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
THROTTLE_PROFILE_ID = os.getenv("THROTTLE_PROFILE_ID", "")
WLAN_NAME = os.getenv("WLAN_NAME", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC = 6_250

STATE_FILE = Path(__file__).parent / "data" / "state.json"
MESSAGES_FILE = Path(__file__).parent / "messages.json"
DYNAMIC_CLIENTS_FILE = Path(__file__).parent / "data" / "dynamic_clients.json"
SETTINGS_FILE = Path(__file__).parent / "data" / "settings.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_telegram_offset = 0
_conversation: dict = {"step": None}
_debug_mode = False


# ---------------------------------------------------------------------------
# Client loading
# ---------------------------------------------------------------------------

def load_clients() -> dict:
    """Parse CLIENT* entries from .env. MACs can be pipe-separated for multi-device."""
    clients = {}
    for key in sorted(k for k in os.environ if k.startswith("CLIENT")):
        raw = os.environ[key]
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) not in (3, 4):
            log.warning("Skipping malformed %s (expected name,mac,limit[,phone]): %s", key, raw)
            continue
        name, macs_str, quota = parts[0], parts[1], parts[2]
        phone = parts[3] if len(parts) == 4 else None
        macs = [m.strip().lower() for m in macs_str.split("|")]
        try:
            limit_seconds = None if quota.lower() == "unlimited" else int(quota.lower().replace("min", "")) * 60
        except ValueError:
            log.warning("Skipping %s — invalid limit %r (use e.g. 60min or unlimited)", key, quota)
            continue
        clients[name] = {"macs": macs, "limit_seconds": limit_seconds, "phone": phone}
    return clients


def load_dynamic_clients() -> dict:
    """Load clients added at runtime via Telegram /add. Migrates old single-mac format."""
    if DYNAMIC_CLIENTS_FILE.exists():
        try:
            with open(DYNAMIC_CLIENTS_FILE) as f:
                data = json.load(f)
            for cfg in data.values():
                if "mac" in cfg and "macs" not in cfg:
                    cfg["macs"] = [cfg.pop("mac")]
            return data
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


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"debug": False}


def save_settings(settings: dict) -> None:
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


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


def _load_state_raw() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


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


def fetch_active_clients() -> dict | None:
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


def _apply_and_reconnect(mac: str, profile_label: str) -> None:
    """Wait for AP to sync the new profile, then kick to force reconnection."""
    log.info("[%s] Waiting 5s for AP to sync profile '%s'.", mac, profile_label)
    time.sleep(5)

    log.info("[%s] Sending kick-sta to force reconnection.", mac)
    _stamgr("kick-sta", mac)

    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(1)
        active = fetch_active_clients()
        if active is None or mac not in active:
            log.info("[%s] Device confirmed disconnected after kick.", mac)
            break
    else:
        log.warning("[%s] Device still visible 15s after kick.", mac)

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
        active = fetch_active_clients()
        if active is not None and mac in active:
            log.info("[%s] Device reconnected with profile '%s' active.", mac, profile_label)
            return
    log.warning("[%s] Device did not reconnect within 30s — may need manual WiFi toggle.", mac)


def throttle_client(mac: str) -> bool:
    ok = _update_user(mac, {"usergroup_id": THROTTLE_PROFILE_ID})
    if ok:
        log.info("[%s] Speed limit profile applied in controller (usergroup_id=%s).", mac, THROTTLE_PROFILE_ID)
        _apply_and_reconnect(mac, "throttled")
    else:
        log.error("[%s] Failed to apply speed limit profile.", mac)
    return ok


def unthrottle_client(mac: str) -> bool:
    ok = _update_user(mac, {"usergroup_id": ""})
    if ok:
        log.info("[%s] Speed limit profile removed in controller.", mac)
        _apply_and_reconnect(mac, "unlimited")
    else:
        log.error("[%s] Failed to remove speed limit profile.", mac)
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


def register_telegram_commands() -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    commands = [
        {"command": "add",     "description": "Legg til enhet"},
        {"command": "modify",  "description": "Endre kvote / blokker / nullstill"},
        {"command": "list",    "description": "Vis alle klienter og bruk"},
        {"command": "debug",   "description": "Aktiver debug-varsler"},
        {"command": "info",    "description": "Deaktiver debug-varsler"},
        {"command": "cancel",  "description": "Avbryt pågående handling"},
    ]
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("Failed to register Telegram commands: %s", e)


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


def send_telegram_buttons(text: str, keyboard: list) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "reply_markup": {"inline_keyboard": keyboard}},
            timeout=10,
        )
    except requests.RequestException as e:
        log.error("Failed to send Telegram buttons: %s", e)


def answer_callback(callback_query_id: str, text: str = "") -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        log.error("Failed to answer callback: %s", e)


def send_debug(text: str) -> None:
    if _debug_mode:
        send_telegram(text)


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


def _remove_from_unknown(mac: str) -> None:
    """Remove a MAC from unknown devices and unthrottle it."""
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


def _do_add(name: str, mac: str, limit_seconds: int | None, phone: str | None,
            clients: dict) -> None:
    """Create a new person with a single MAC."""
    new_client = {"macs": [mac], "limit_seconds": limit_seconds, "phone": phone}
    clients[name] = new_client
    dynamic = load_dynamic_clients()
    dynamic[name] = new_client
    save_dynamic_clients(dynamic)
    _remove_from_unknown(mac)
    add_to_wlan_allowlist(mac)
    limit_str = f"{limit_seconds // 60}min" if limit_seconds else "ubegrenset"
    sms_str = f"SMS til {phone}" if phone else "ingen SMS"
    send_telegram(
        f"✅ {name} er lagt til!\n"
        f"• Grense: {limit_str}\n"
        f"• {sms_str}"
    )
    log.info("Added %s (%s) via Telegram.", name, mac)


def _do_add_mac(name: str, mac: str, clients: dict) -> None:
    """Add an extra MAC to an existing person."""
    clients[name]["macs"].append(mac)
    dynamic = load_dynamic_clients()
    entry = dynamic.get(name) or dict(clients[name])
    entry["macs"] = clients[name]["macs"]
    dynamic[name] = entry
    save_dynamic_clients(dynamic)
    _remove_from_unknown(mac)
    add_to_wlan_allowlist(mac)
    send_telegram(f"✅ Ny enhet ({mac}) lagt til for {name}.")
    log.info("Added extra MAC %s to %s via Telegram.", mac, name)


def handle_callback_query(callback_query_id: str, data: str, clients: dict) -> None:
    global _conversation
    parts = data.split(":")

    def _mac_from_flat(flat: str) -> str:
        return ":".join(flat[i:i+2] for i in range(0, 12, 2))

    if parts[0] == "assign" and len(parts) == 3:
        mac, name = _mac_from_flat(parts[1]), parts[2]
        if name not in clients:
            answer_callback(callback_query_id, "Klient ikke funnet.")
            return
        answer_callback(callback_query_id)
        _do_add_mac(name, mac, clients)

    elif parts[0] == "assign_new" and len(parts) == 2:
        mac = _mac_from_flat(parts[1])
        answer_callback(callback_query_id)
        _conversation = {"step": "awaiting_name", "preset_mac": mac}
        send_telegram(f"Hva skal personen hete?\n(MAC {mac} legges til automatisk)")

    elif parts[0] == "add_person" and len(parts) == 2:
        name = parts[1]
        if name not in clients:
            answer_callback(callback_query_id, "Klient ikke funnet.")
            return
        answer_callback(callback_query_id)
        _conversation = {"step": "awaiting_add_mac", "target": name}
        send_telegram(f"MAC-adresse til {name}s nye enhet?\n(format: aa:bb:cc:dd:ee:ff)")

    elif parts[0] == "add_new":
        answer_callback(callback_query_id)
        _conversation = {"step": "awaiting_name"}
        send_telegram("Hva skal personen hete? (f.eks. dag_kone, ingen mellomrom)")

    elif parts[0] == "modify" and len(parts) == 2:
        name = parts[1]
        if name not in clients:
            answer_callback(callback_query_id, "Klient ikke funnet.")
            return
        answer_callback(callback_query_id)
        cfg = clients[name]
        limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "ubegrenset"
        n = len(cfg["macs"])
        device_str = f"{n} enhet{'er' if n > 1 else ''}"
        keyboard = [
            [{"text": "✏️ Endre kvote",      "callback_data": f"action:{name}:quota"}],
            [{"text": "🚫 Blokker",          "callback_data": f"action:{name}:block"}],
            [{"text": "🔄 Nullstill teller", "callback_data": f"action:{name}:reset"}],
        ]
        send_telegram_buttons(f"{name} — {device_str} — kvote: {limit_str}\nHva vil du gjøre?", keyboard)

    elif parts[0] == "action" and len(parts) == 3:
        name, action = parts[1], parts[2]
        if name not in clients:
            answer_callback(callback_query_id, "Klient ikke funnet.")
            return
        macs = clients[name]["macs"]
        state = _load_state_raw()

        if action == "quota":
            answer_callback(callback_query_id)
            _conversation = {"step": "awaiting_quota_minutes", "target": name}
            send_telegram(f"Ny daglig kvote for {name}?\nSkriv antall minutter eller 'unlimited':")

        elif action == "block":
            for mac in macs:
                throttle_client(mac)
            state.setdefault(name, {})["throttled"] = True
            save_state(state)
            answer_callback(callback_query_id, "🚫 Blokkert!")
            send_telegram(f"🚫 {name} er blokkert. Bruk Nullstill for å åpne igjen.")
            log.info("%s manually throttled via Telegram.", name)

        elif action == "reset":
            for mac in macs:
                unthrottle_client(mac)
            state[name] = {"seconds": 0, "throttled": False, "notified_half": False}
            save_state(state)
            answer_callback(callback_query_id, "🔄 Nullstilt!")
            send_telegram(f"🔄 {name} er nullstilt og frigjort.")
            log.info("%s reset via Telegram.", name)


def handle_telegram_command(text: str, clients: dict) -> None:
    global _conversation, _debug_mode
    text = text.strip()
    cmd = text.lower().split()[0] if text else ""

    if cmd == "/cancel":
        _conversation = {"step": None}
        send_telegram("❌ Avbrutt.")
        return

    step = _conversation.get("step")

    if step is None:
        if cmd == "/add":
            if clients:
                keyboard = [[{"text": name, "callback_data": f"add_person:{name}"}]
                            for name in clients]
                keyboard.append([{"text": "➕ Ny person", "callback_data": "add_new"}])
                send_telegram_buttons("Legg til enhet for hvem?", keyboard)
            else:
                _conversation = {"step": "awaiting_name"}
                send_telegram("Hva skal personen hete? (f.eks. dag_kone, ingen mellomrom)")

        elif cmd == "/modify":
            if not clients:
                send_telegram("Ingen klienter å endre.")
                return
            keyboard = [[{"text": name, "callback_data": f"modify:{name}"}] for name in clients]
            send_telegram_buttons("Velg person å endre:", keyboard)

        elif cmd == "/list":
            lines = []
            if clients:
                dynamic_names = load_dynamic_clients().keys()
                state = _load_state_raw()
                lines.append("📋 Kjente klienter:\n")
                for name, cfg in clients.items():
                    limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "ubegrenset"
                    source = "(lagt til)" if name in dynamic_names else "(.env)"
                    phone_str = cfg["phone"] if cfg.get("phone") else "ingen SMS"
                    n = len(cfg["macs"])
                    device_str = f"{n} enhet{'er' if n > 1 else ''}"
                    person = state.get(name, {})
                    used_min = person.get("seconds", 0) // 60
                    throttled = person.get("throttled", False)
                    if cfg["limit_seconds"]:
                        status = f"🚫 {used_min}min brukt" if throttled else f"{used_min}/{cfg['limit_seconds'] // 60}min"
                    else:
                        status = f"{used_min}min brukt (ubegrenset)"
                    lines.append(f"• {name} — {status} — {device_str} — {phone_str} {source}")
            if STATE_FILE.exists():
                try:
                    with open(STATE_FILE) as f:
                        st = json.load(f)
                    unknowns = st.get("unknown_devices", {})
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

        elif cmd == "/debug":
            _debug_mode = True
            settings = load_settings()
            settings["debug"] = True
            save_settings(settings)
            send_telegram("🔍 Debug-modus aktivert. Du får nå varsler ved blokkering og hvert 10. minutt.")
            log.info("Debug mode enabled via Telegram.")

        elif cmd == "/info":
            _debug_mode = False
            settings = load_settings()
            settings["debug"] = False
            save_settings(settings)
            send_telegram("ℹ️ Normal modus gjenopprettet.")
            log.info("Debug mode disabled via Telegram.")

        else:
            send_telegram(
                "Tilgjengelige kommandoer:\n"
                "/add — legg til enhet\n"
                "/modify — endre kvote/blokker/nullstill\n"
                "/list — vis alle\n"
                "/debug — aktiver debug-varsler\n"
                "/info — deaktiver debug-varsler"
            )
        return

    # --- Active conversation steps ---
    if step == "awaiting_name":
        if not text or " " in text:
            send_telegram("❌ Navn kan ikke inneholde mellomrom. Prøv igjen:")
            return
        _conversation["name"] = text
        if "preset_mac" in _conversation:
            _conversation["step"] = "awaiting_limit"
            send_telegram("Tidsgrense per dag?\n• 60min\n• unlimited")
        else:
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
            mac=_conversation.get("preset_mac") or _conversation.get("mac"),
            limit_seconds=_conversation["limit_seconds"],
            phone=phone,
            clients=clients,
        )
        _conversation = {"step": None}

    elif step == "awaiting_add_mac":
        if not _valid_mac(text):
            send_telegram("❌ Ugyldig MAC-adresse. Format: aa:bb:cc:dd:ee:ff\nPrøv igjen:")
            return
        target = _conversation.get("target")
        if target and target in clients:
            _do_add_mac(target, text.lower(), clients)
        else:
            send_telegram("❌ Klient ikke funnet.")
        _conversation = {"step": None}

    elif step == "awaiting_quota_minutes":
        target = _conversation.get("target")
        try:
            limit_seconds = _parse_limit(text)
        except ValueError:
            send_telegram("❌ Skriv f.eks. 60min eller unlimited:")
            return
        if target and target in clients:
            clients[target]["limit_seconds"] = limit_seconds
            dynamic = load_dynamic_clients()
            entry = dynamic.get(target) or dict(clients[target])
            entry["limit_seconds"] = limit_seconds
            dynamic[target] = entry
            save_dynamic_clients(dynamic)
            limit_str = f"{limit_seconds // 60}min" if limit_seconds else "ubegrenset"
            send_telegram(f"✅ Kvote for {target} endret til {limit_str}.")
            log.info("Quota for %s updated to %s via Telegram.", target, limit_str)
        else:
            send_telegram("❌ Klient ikke funnet.")
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

            cb = update.get("callback_query")
            if cb:
                chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if chat_id == TELEGRAM_CHAT_ID:
                    handle_callback_query(cb["id"], cb.get("data", ""), clients)
                else:
                    log.warning("Ignoring callback from unknown chat ID %s.", chat_id)
                continue

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
        for mac in cfg["macs"]:
            _stamgr("unblock-sta", mac)
            if unthrottle_client(mac):
                log.info("Unthrottled %s (%s).", name, mac)
    fresh = {name: {"seconds": 0, "throttled": False, "notified_half": False} for name in clients}
    fresh["last_reset_date"] = date.today().isoformat()
    fresh["unknown_devices"] = state.get("unknown_devices", {})
    save_state(fresh)
    log.info("Reset complete.")


def handle_unknown_devices(active_network_clients: dict, clients: dict, state: dict) -> None:
    known_macs = {mac for cfg in clients.values() for mac in cfg["macs"]}
    unknown_devices = state.setdefault("unknown_devices", {})
    pending = state.setdefault("pending_unknown", {})

    # Remove pending entries that are no longer on the network (brief visitors)
    for mac in list(pending):
        if mac not in active_network_clients:
            log.info("Pending unknown %s left before second poll — ignoring.", mac)
            del pending[mac]

    for mac, client in active_network_clients.items():
        if mac in known_macs or mac in unknown_devices:
            continue
        hostname = client.get("hostname") or client.get("name") or mac
        if mac not in pending:
            log.info("Unknown device first seen: %s (%s) — waiting for next poll to confirm.", hostname, mac)
            pending[mac] = {"hostname": hostname}
        else:
            # Seen two polls in a row — it's staying, throttle and notify
            log.info("Unknown device confirmed: %s (%s) — throttling.", hostname, mac)
            throttle_client(mac)
            del pending[mac]
            unknown_devices[mac] = {"hostname": hostname, "first_seen": date.today().isoformat()}
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                mac_flat = mac.replace(":", "")
                keyboard = [[{"text": name, "callback_data": f"assign:{mac_flat}:{name}"}]
                            for name in clients]
                keyboard.append([{"text": "➕ Ny person", "callback_data": f"assign_new:{mac_flat}"}])
                send_telegram_buttons(
                    f"Ukjent enhet på nettverket:\n  {hostname} ({mac})\nHvem eier den?",
                    keyboard
                )


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
        macs = cfg["macs"]
        limit_seconds = cfg["limit_seconds"]
        phone = cfg["phone"]
        person = state.setdefault(name, {"seconds": 0, "throttled": False, "notified_half": False})

        if person["throttled"]:
            log.info("%s already throttled, skipping.", name)
            continue

        # Migrate tx_bytes from old scalar format to per-MAC dict
        if isinstance(person.get("tx_bytes"), (int, float)):
            person["tx_bytes"] = {}
        tx_bytes_map = person.setdefault("tx_bytes", {})

        total_delta = 0
        idle_rates = []
        active_mac_count = 0
        any_on_network = False
        waiting_for_baseline = False

        for mac in macs:
            client = active_network_clients.get(mac)
            if client is None:
                tx_bytes_map.pop(mac, None)
                continue

            any_on_network = True
            tx_bytes = client.get("tx_bytes") or 0
            prev = tx_bytes_map.get(mac)
            tx_bytes_map[mac] = tx_bytes

            if prev is None:
                waiting_for_baseline = True
                continue

            delta = tx_bytes - prev
            if delta < 0:
                log.info("%s (%s) tx_bytes counter reset, skipping interval.", name, mac)
                continue

            avg_rate = delta / POLL_INTERVAL_SECONDS
            if avg_rate >= ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC:
                total_delta += delta
                active_mac_count += 1
            else:
                idle_rates.append(avg_rate)

        if not any_on_network:
            log.info("%s not on network.", name)
            continue

        if waiting_for_baseline and active_mac_count == 0:
            log.info("%s first seen — waiting for next poll to measure usage.", name)
            continue

        if active_mac_count == 0:
            avg_idle = sum(idle_rates) / len(idle_rates) if idle_rates else 0
            log.info("%s idle (avg %.0f bytes/s), not counting.", name, avg_idle)
            continue

        # At least one device is active — count one poll interval
        person["seconds"] += POLL_INTERVAL_SECONDS
        minutes_used = person["seconds"] // 60
        total_avg_rate = total_delta / POLL_INTERVAL_SECONDS

        if limit_seconds is None:
            log.info("%s active — %dm used (unlimited) [avg dl %.0f bytes/s].", name, minutes_used, total_avg_rate)
            continue

        limit_minutes = limit_seconds // 60
        log.info("%s active — %dm/%dm used [avg dl %.0f bytes/s].", name, minutes_used, limit_minutes, total_avg_rate)

        # Debug: 10-minute milestones
        last_milestone = person.get("last_10min_milestone", 0)
        current_milestone = (minutes_used // 10) * 10
        if current_milestone > last_milestone and current_milestone > 0:
            person["last_10min_milestone"] = current_milestone
            send_debug(f"⏱ {name}: {minutes_used}/{limit_minutes}min brukt")

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

        # Throttle all devices when quota is reached
        if person["seconds"] >= limit_seconds:
            log.info("Limit reached — throttling all devices for %s.", name)
            throttled_any = False
            for mac in macs:
                if throttle_client(mac):
                    throttled_any = True
            if throttled_any:
                person["throttled"] = True
                msg = _pick_message(messages, "throttled",
                                    name=name.capitalize(), used=minutes_used,
                                    limit=limit_minutes)
                if msg:
                    send_sms(phone, msg)
                    log.info("Throttle notification sent to %s.", name)
                send_debug(f"🚫 {name} er blokkert etter {minutes_used}min.")

    save_state(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _debug_mode

    if not UDM_IP or not API_KEY:
        log.error("UDM_IP and UNIFI_API_KEY must be set in .env")
        sys.exit(1)

    STATE_FILE.parent.mkdir(exist_ok=True)

    settings = load_settings()
    _debug_mode = settings.get("debug", False)

    clients = load_clients()
    clients.update(load_dynamic_clients())
    if not clients:
        log.error("No clients configured. Add clients via Telegram /add or CLIENT* entries in .env.")
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

    log.info("Starting internet blocker. Debug mode: %s.", "ON" if _debug_mode else "off")
    for name, cfg in clients.items():
        limit_str = f"{cfg['limit_seconds'] // 60}min" if cfg["limit_seconds"] else "unlimited"
        sms_str = cfg["phone"] if cfg["phone"] else "no SMS"
        macs_str = ", ".join(cfg["macs"])
        log.info("  %s — %s — limit: %s — %s", name, macs_str, limit_str, sms_str)
    vlan_str = f"VLAN{VLAN_ID}" if VLAN_ID else "all VLANs"
    log.info("Polling %s every %ds | Throttle profile: %s | Active threshold: %d bytes/s",
             vlan_str, POLL_INTERVAL_SECONDS, THROTTLE_PROFILE_ID or "NOT SET",
             ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC)
    if TELEGRAM_BOT_TOKEN:
        log.info("Telegram bot active — listening for admin commands.")
        register_telegram_commands()

    while True:
        check_telegram(clients)

        state = load_state(clients)
        if state.get("last_reset_date") != date.today().isoformat():
            run_reset(clients)

        run_monitor(clients, messages)


if __name__ == "__main__":
    main()
