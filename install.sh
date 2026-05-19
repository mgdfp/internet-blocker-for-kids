#!/usr/bin/env bash
# Install and enable the internet-blocker systemd user service.
# Run once after cloning. Re-run after changing the unit file.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill in your values first."
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

loginctl enable-linger "$USER"

ln -sf "$REPO_DIR/systemd/internet-blocker.service" "$SYSTEMD_USER_DIR/internet-blocker.service"
echo "Linked internet-blocker.service"

systemctl --user daemon-reload
systemctl --user enable --now internet-blocker.service

echo ""
echo "Done. Service status:"
systemctl --user status internet-blocker.service
