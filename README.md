# Internet Blocker for Kids

Enforces a daily 1-hour screen time limit for specific devices on a UniFi UDM Pro network.
Polls the UniFi API every 5 minutes, tracks active usage in a local JSON file, and blocks devices when they hit the limit. Resets automatically at midnight.

## How it works

- Only counts a device as "active" if its download rate exceeds `ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC` (default: 6250 bytes/s = 50 Kbps). Idle background traffic doesn't burn the quota.
- State is stored in `data/state.json`.
- Two systemd timers: one fires every 5 minutes (monitor), one fires at midnight (reset).
- If the machine is off at midnight, `Persistent=true` ensures the reset fires on next boot.

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
   - Copy `.env.example` to `.env` and fill in your UDM Pro IP and API key.
   - Edit `KIDS` in `internet_blocker.py` with your kids' names and real MAC addresses.

## Server Setup

1. **Symlink systemd files:**
   ```
   ln -s ~/src/internet-blocker-for-kids/systemd/internet-blocker.service ~/.config/systemd/user/
   ln -s ~/src/internet-blocker-for-kids/systemd/internet-blocker.timer ~/.config/systemd/user/
   ln -s ~/src/internet-blocker-for-kids/systemd/internet-blocker-reset.service ~/.config/systemd/user/
   ln -s ~/src/internet-blocker-for-kids/systemd/internet-blocker-reset.timer ~/.config/systemd/user/
   ```

2. **Enable and start:**
   ```
   systemctl --user daemon-reload
   systemctl --user enable --now internet-blocker.timer
   systemctl --user enable --now internet-blocker-reset.timer
   sudo loginctl enable-linger $USER
   ```

## Monitoring

```
# View active timers
systemctl --user list-timers

# Tail logs
journalctl --user -u internet-blocker.service -f
journalctl --user -u internet-blocker-reset.service -f

# Manual run (monitor)
systemctl --user start internet-blocker.service

# Manual reset
systemctl --user start internet-blocker-reset.service
```

## Tuning

| Variable | Default | Description |
|---|---|---|
| `DAILY_LIMIT_MINUTES` | 60 | Daily screen time quota per device |
| `POLL_INTERVAL_MINUTES` | 5 | Must match the timer interval |
| `ACTIVE_RATE_THRESHOLD_BYTES_PER_SEC` | 6250 | 50 Kbps — raise if idle sync triggers false counts |
