#!/bin/bash
#
# Restore blink-to-telegram state from OCI Object Storage onto this VM.
#
# Run as root on a freshly installed VM (after scripts/install.sh):
#     sudo scripts/restore_from_oci.sh
#
# Requires the OCI CLI and instance-principal access to the backup bucket
# (the "blink-backup-dg" dynamic group + "blink-backup-policy" IAM policy).
#
set -euo pipefail

BUCKET=${BUCKET:-blink-forwarder-state}
DATA_DIR=${DATA_DIR:-/var/lib/blink-to-telegram}
ENV_FILE=${ENV_FILE:-/etc/blink-to-telegram/env}
OCI_BIN=${OCI:-oci}
AUTH=${AUTH:-instance_principal}

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

echo "==> stopping the forwarder cron job while restoring"
if [[ -f /etc/cron.d/blink-forwarder ]]; then
    mv /etc/cron.d/blink-forwarder /etc/cron.d/blink-forwarder.disabled
fi

fetch() {
    # fetch <object-name> <destination> <owner>
    local name="$1" dest="$2" owner="$3"
    local tmp
    tmp=$(mktemp)
    "$OCI_BIN" os object get --bucket-name "$BUCKET" --name "$name" \
        --file "$tmp" --auth "$AUTH"
    install -o "$owner" -g "$owner" -m 600 "$tmp" "$dest"
    rm -f "$tmp"
    echo "restored $name -> $dest"
}

echo "==> restoring state database"
rm -f "$DATA_DIR/state.db-wal" "$DATA_DIR/state.db-shm"
fetch state.db "$DATA_DIR/state.db" blink

if "$OCI_BIN" os object head --bucket-name "$BUCKET" --name blink_login.json \
        --auth "$AUTH" >/dev/null 2>&1; then
    echo "==> restoring Blink login (no 2FA needed)"
    fetch blink_login.json "$DATA_DIR/blink_login.json" blink
else
    echo "==> no saved Blink login in the bucket; run blink-login manually"
fi

echo "==> restoring configuration"
fetch env "$ENV_FILE" blink

echo "==> re-enabling the forwarder cron job"
if [[ -f /etc/cron.d/blink-forwarder.disabled ]]; then
    mv /etc/cron.d/blink-forwarder.disabled /etc/cron.d/blink-forwarder
fi

echo "Restore complete."
