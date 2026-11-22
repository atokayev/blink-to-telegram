#!/usr/bin/env bash
set -euo pipefail

set -a
# shellcheck disable=SC1091
. /etc/blink-to-telegram/env
set +a

cd /opt/blink-to-telegram
exec .venv/bin/python scripts/blink_login.py "$@"
