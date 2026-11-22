#!/usr/bin/env bash
#
# Credential-free local smoke test. Run from anywhere:
#   scripts/local_test.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PYTHON="$REPO_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "No virtualenv found. Create one first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 1
fi

echo "==> Byte-compiling"
"$PYTHON" -m compileall -q blink_forwarder scripts tests

echo "==> Running unit tests"
"$PYTHON" -m unittest discover -s tests -v

echo "==> Checking the CLI"
"$PYTHON" -m blink_forwarder --version

echo
echo "Local smoke tests passed."
echo "Next (needs credentials):"
echo "  1. cp .env.example .env   # fill in the Telegram token and chat id"
echo "  2. $PYTHON -m blink_forwarder --check"
echo "  3. $PYTHON scripts/blink_login.py"
echo "  4. $PYTHON -m blink_forwarder --dry-run"
echo "  5. $PYTHON -m blink_forwarder --max-clips 1"
