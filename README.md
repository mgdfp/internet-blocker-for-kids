# Internet Blocker for Kids

Enforces a daily 1-hour screen time limit for specific devices on a UniFi UDM Pro network.
Runs as a daemon, polling the UniFi API every 5 minutes. Resets automatically at midnight.

## How it works

- Only counts a device as "active" if its download rate exceeds `ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC` (default: 6250 bytes/s = 50 Kbps). Idle background traffic doesn't burn the quota.
- State is stored in `data/state.json`.
- Midnight reset is automatic — the daemon detects the date change and unblocks all devices.
- If the machine was off at midnight, the reset fires on next startup.

## Local Setup

1. **Install uv:**
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone and sync:**
   ```
   cd internet-blocker-for-kids
   uv sync
   ```

3. **Configure:**
   Copy `.env.example` to `.env` and fill in your values. See `.env.example` for the full format.

## Server Setup

1. **Symlink systemd file:**
   ```
   ln -s ~/src/internet-blocker-for-kids/systemd/internet-blocker.service ~/.config/systemd/user/
   ```

2. **Enable and start:**
   ```
   systemctl --user daemon-reload
   systemctl --user enable --now internet-blocker.service
   sudo loginctl enable-linger $USER
   ```

## Monitoring

```bash
# Tail live logs
journalctl --user -u internet-blocker.service -f

# Manual reset (unblock all, zero counters)
uv run internet_blocker.py --reset

# Restart the daemon
systemctl --user restart internet-blocker.service
```

## Tuning

| Variable | Default | Description |
|---|---|---|
| `DAILY_LIMIT_MINUTES` | 60 | Daily screen time quota per device |
| `POLL_INTERVAL_SECONDS` | 300 | How often to check (5 minutes) |
| `ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC` | 6250 | 50 Kbps — raise if idle sync triggers false counts |
