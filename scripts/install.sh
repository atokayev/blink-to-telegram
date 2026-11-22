#!/usr/bin/env bash
#
# Install the Blink -> Telegram forwarder on an Ubuntu VM (Oracle Cloud
# Always Free is the target).  Run from the repository checkout with sudo.
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/blink-to-telegram}"
CONF_DIR="${CONF_DIR:-/etc/blink-to-telegram}"
DATA_DIR="${DATA_DIR:-/var/lib/blink-to-telegram}"
LOG_FILE="${LOG_FILE:-/var/log/blink-forwarder.log}"
SERVICE_USER="${SERVICE_USER:-blink}"
USE_SYSTEMD=0

usage() {
    cat <<'EOF'
Usage: sudo scripts/install.sh [--systemd]

  (default)  install a cron job that runs every 5 minutes
  --systemd  install a systemd service + timer instead
EOF
}

for arg in "$@"; do
    case "$arg" in
        --systemd) USE_SYSTEMD=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script as root: sudo $0 $*" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync util-linux cron sqlite3

echo "==> Creating service user and directories"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
    "$DATA_DIR" "$DATA_DIR/archive" "$DATA_DIR/tmp"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONF_DIR"

echo "==> Installing application into $APP_DIR"
install -d -o root -g root -m 0755 "$APP_DIR"
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    rsync -a --delete \
        --exclude '.git' \
        --exclude '.venv' \
        --exclude 'data' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '*.egg-info' \
        "$REPO_DIR/" "$APP_DIR/"
fi

echo "==> Creating virtualenv and installing dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"
chown -R root:root "$APP_DIR"

echo "==> Installing configuration"
if [[ ! -f "$CONF_DIR/env" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0600 \
        "$APP_DIR/deploy/env.example" "$CONF_DIR/env"
    echo "Created $CONF_DIR/env - edit it with your Telegram token and chat id."
else
    echo "Keeping existing $CONF_DIR/env"
fi

echo "==> Installing helper commands"
install -o root -g root -m 0755 "$APP_DIR/deploy/blink-forwarder.sh" /usr/local/bin/blink-forwarder
install -o root -g root -m 0755 "$APP_DIR/deploy/blink-login.sh" /usr/local/bin/blink-login

touch "$LOG_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_FILE"
chmod 0640 "$LOG_FILE"
install -o root -g root -m 0644 "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/blink-forwarder

if [[ "$USE_SYSTEMD" -eq 1 ]]; then
    echo "==> Installing systemd service and timer"
    install -o root -g root -m 0644 "$APP_DIR/deploy/blink-forwarder.service" /etc/systemd/system/
    install -o root -g root -m 0644 "$APP_DIR/deploy/blink-forwarder.timer" /etc/systemd/system/
    rm -f /etc/cron.d/blink-forwarder
    systemctl daemon-reload
    systemctl enable --now blink-forwarder.timer
else
    echo "==> Installing cron job"
    install -o root -g root -m 0644 "$APP_DIR/deploy/cron.d" /etc/cron.d/blink-forwarder
    systemctl enable --now cron >/dev/null 2>&1 || true
    systemctl disable --now blink-forwarder.timer >/dev/null 2>&1 || true
fi

cat <<EOF

Installation complete.

Next steps:
  1. Edit the configuration:
       sudo nano $CONF_DIR/env

  2. Authenticate Blink (handles the 2FA prompt):
       sudo -u $SERVICE_USER blink-login

  3. Validate Telegram and the saved login:
       sudo -u $SERVICE_USER blink-forwarder --check

  4. Preview what would be forwarded (no uploads):
       sudo -u $SERVICE_USER blink-forwarder --dry-run

  5. Run once for real:
       sudo -u $SERVICE_USER blink-forwarder

  6. Watch the scheduled job:
       tail -f $LOG_FILE

The job runs every 5 minutes. Change the schedule in
/etc/cron.d/blink-forwarder (or /etc/systemd/system/blink-forwarder.timer).
EOF
