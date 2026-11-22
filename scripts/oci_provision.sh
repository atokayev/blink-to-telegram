#!/usr/bin/env bash
#
# Provision and bootstrap the Blink -> Telegram forwarder on Oracle Cloud
# Always Free, end to end:
#
#   network (VCN, IGW, route, subnet, SSH-only security list)
#   compute (VM.Standard.E2.1.Micro, Ubuntu 24.04, capacity-retry loop)
#   backups (private Object Storage bucket + instance-principal IAM)
#   optional --bootstrap: sync this repo to the VM, install, disable cron,
#                         copy local .env values, install the backup cron
#
# Idempotent: re-running reuses everything that already exists.
#
# Usage:
#   scripts/oci_provision.sh [--bootstrap]
#
# Requires: OCI CLI configured (https://docs.oracle.com/.../oci-cli),
#           ssh-keygen, rsync.
#
set -euo pipefail

SHAPE="${SHAPE:-VM.Standard.E2.1.Micro}"
BOOTSTRAP=0
for arg in "$@"; do
    case "$arg" in
        --bootstrap) BOOTSTRAP=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${HOME}/.oci/blink_stack.env"

# ---------------------------------------------------------------------------
# locate the CLI
# ---------------------------------------------------------------------------
OCI_BIN="${OCI:-}"
if [[ -z "$OCI_BIN" ]]; then
    if command -v oci >/dev/null 2>&1; then
        OCI_BIN="$(command -v oci)"
    elif [[ -x /opt/homebrew/bin/oci ]]; then
        OCI_BIN=/opt/homebrew/bin/oci
    elif [[ -x /usr/local/bin/oci ]]; then
        OCI_BIN=/usr/local/bin/oci
    else
        echo "OCI CLI not found. Install it first: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm" >&2
        exit 1
    fi
fi
echo "==> OCI CLI: $OCI_BIN ($("$OCI_BIN" --version))"

# ---------------------------------------------------------------------------
# authenticate and pick up tenancy / region from ~/.oci/config
# ---------------------------------------------------------------------------
CONFIG_FILE="${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}"
TENANCY="$(grep -E '^tenancy' "$CONFIG_FILE" | head -1 | cut -d= -f2 | xargs)"
REGION="$(grep -E '^region' "$CONFIG_FILE" | head -1 | cut -d= -f2 | xargs)"
[[ -n "$TENANCY" ]] || { echo "No 'tenancy' entry in $CONFIG_FILE" >&2; exit 1; }

echo "==> verifying authentication"
"$OCI_BIN" os ns get >/dev/null
echo "    authenticated; tenancy=$TENANCY"

HOME_REGION="$("$OCI_BIN" iam region-subscription list \
    --query 'data[?"is-home-region" == true]."region-name" | [0]' --raw-output 2>/dev/null || true)"
if [[ -n "$HOME_REGION" && "$HOME_REGION" != "$REGION" ]]; then
    echo "WARNING: config region ($REGION) is not the home region ($HOME_REGION)." >&2
    echo "         Always Free resources must live in the home region." >&2
fi
echo "==> region: $REGION (home: ${HOME_REGION:-unknown})"

ADS="$("$OCI_BIN" iam availability-domain list --query 'data[].name' --raw-output 2>/dev/null)"
[[ -n "$ADS" ]] || { echo "Could not list availability domains; retry in a minute (IAM propagation)." >&2; exit 1; }
echo "==> availability domains: $(echo "$ADS" | tr -d '[]" ' | tr ',' ' ')"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
save() { echo "$1=$2" >> "$STATE_FILE"; }
: > "$STATE_FILE"

json_get() { "$OCI_BIN" "$@" --raw-output 2>/dev/null | tr -d '"'; }

# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------
echo "==> network"
VCN="$(json_get network vcn list --compartment-id "$TENANCY" \
    --query "data[?\"display-name\"=='blink-vcn'].id | [0]")"
if [[ -z "$VCN" ]]; then
    VCN="$(json_get network vcn create --compartment-id "$TENANCY" \
        --display-name blink-vcn --cidr-blocks '["10.0.0.0/16"]' --dns-label blinkvcn \
        --wait-for-state AVAILABLE --query 'data.id')"
    echo "    created VCN"
else
    echo "    reusing VCN"
fi
save VCN "$VCN"

IGW="$(json_get network internet-gateway list --compartment-id "$TENANCY" --vcn-id "$VCN" \
    --query "data[?\"display-name\"=='blink-igw' && \"is-enabled\"==true].id | [0]")"
if [[ -z "$IGW" ]]; then
    IGW="$(json_get network internet-gateway create --compartment-id "$TENANCY" \
        --vcn-id "$VCN" --is-enabled true --display-name blink-igw --query 'data.id')"
    echo "    created internet gateway"
else
    echo "    reusing internet gateway"
fi
save IGW "$IGW"

ROUTE_TABLE="$(json_get network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"')"
if ! "$OCI_BIN" network route-table get --rt-id "$ROUTE_TABLE" \
        --query 'data."route-rules"[]."network-entity-id"' --raw-output 2>/dev/null | grep -q "$IGW"; then
    "$OCI_BIN" network route-table update --rt-id "$ROUTE_TABLE" --force \
        --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" \
        >/dev/null
    echo "    default route -> internet gateway"
fi
save ROUTE_TABLE "$ROUTE_TABLE"

SECURITY_LIST="$(json_get network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"')"
if ! "$OCI_BIN" network security-list get --security-list-id "$SECURITY_LIST" \
        --query 'data."ingress-security-rules"' --output json 2>/dev/null \
        | grep -q '"min": 22'; then
    "$OCI_BIN" network security-list update --security-list-id "$SECURITY_LIST" --force \
        --ingress-security-rules '[{"protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},{"protocol":"1","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK","icmpOptions":{"type":3,"code":4}}]' \
        >/dev/null
    echo "    SSH-only ingress rule added"
else
    echo "    SSH ingress already open (verified)"
fi
save SECURITY_LIST "$SECURITY_LIST"

SUBNET="$(json_get network subnet list --compartment-id "$TENANCY" --vcn-id "$VCN" \
    --query "data[?\"display-name\"=='blink-public'].id | [0]")"
if [[ -z "$SUBNET" ]]; then
    SUBNET="$(json_get network subnet create --compartment-id "$TENANCY" --vcn-id "$VCN" \
        --cidr-block 10.0.0.0/24 --display-name blink-public --dns-label blinkpublic \
        --route-table-id "$ROUTE_TABLE" --security-list-ids "[\"$SECURITY_LIST\"]" \
        --prohibit-public-ip-on-vnic false --wait-for-state AVAILABLE --query 'data.id')"
    echo "    created public subnet"
else
    echo "    reusing subnet"
fi
save SUBNET "$SUBNET"

# ---------------------------------------------------------------------------
# ssh key
# ---------------------------------------------------------------------------
SSH_KEY="${HOME}/.oci/blink_oci"
if [[ ! -f "$SSH_KEY" ]]; then
    mkdir -p "$(dirname "$SSH_KEY")"
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "blink-to-telegram-vm" -q
    echo "==> generated SSH key $SSH_KEY.pub"
fi

# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------
echo "==> finding the latest Ubuntu 24.04 image for $SHAPE"
IMAGE="$(json_get compute image list --compartment-id "$TENANCY" --all \
    --shape "$SHAPE" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
    --sort-by TIMECREATED --sort-order DESC --query 'data[0].id')"
[[ -n "$IMAGE" ]] || { echo "No Ubuntu 24.04 image found for this shape/region." >&2; exit 1; }
echo "    image: $IMAGE"

# ---------------------------------------------------------------------------
# compute instance (capacity retry loop)
# ---------------------------------------------------------------------------
INSTANCE="$(json_get compute instance list --compartment-id "$TENANCY" --all \
    --query "data[?\"display-name\"=='blink-forwarder' && \"lifecycle-state\"=='RUNNING'].id | [0]")"
if [[ -n "$INSTANCE" ]]; then
    echo "==> reusing running instance"
else
    echo "==> launching instance (retrying across ADs on capacity errors)"
    AD_LIST="$(echo "$ADS" | tr -d '[]" ' | tr ',' ' ')"
    OUT=""
    for attempt in 1 2 3 4 5 6; do
        for AD in $AD_LIST; do
            echo "    attempt $attempt in $AD"
            if OUT="$("$OCI_BIN" compute instance launch \
                --availability-domain "$AD" \
                --compartment-id "$TENANCY" \
                --shape "$SHAPE" \
                --image-id "$IMAGE" \
                --subnet-id "$SUBNET" \
                --assign-public-ip true \
                --ssh-authorized-keys-file "$SSH_KEY.pub" \
                --display-name blink-forwarder \
                --output json 2>&1)"; then
                break 2
            fi
            echo "$OUT" | grep -o '"message": "[^"]*"' | head -1 || true
            sleep 30
        done
    done
    INSTANCE="$(echo "$OUT" | grep -o '"id": "ocid1\.instance[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$INSTANCE" ]] || { echo "Instance launch failed after retries." >&2; exit 1; }
fi
save INSTANCE "$INSTANCE"

echo "==> waiting for RUNNING state"
for _ in $(seq 1 30); do
    STATE="$(json_get compute instance get --instance-id "$INSTANCE" --query 'data."lifecycle-state"')"
    [[ "$STATE" == "RUNNING" ]] && break
    sleep 10
done
[[ "$STATE" == "RUNNING" ]] || { echo "Instance did not reach RUNNING." >&2; exit 1; }

PUBLIC_IP="$("$OCI_BIN" compute instance list-vnics --instance-id "$INSTANCE" \
    --query 'data[0]."public-ip"' --raw-output 2>/dev/null | tr -d '"')"
[[ -n "$PUBLIC_IP" ]] || { echo "No public IP assigned." >&2; exit 1; }
save PUBLIC_IP "$PUBLIC_IP"
echo "    public IP: $PUBLIC_IP"

# ---------------------------------------------------------------------------
# backup bucket + instance-principal IAM
# ---------------------------------------------------------------------------
BUCKET=blink-forwarder-state
echo "==> object storage bucket"
if "$OCI_BIN" os bucket get --bucket-name "$BUCKET" >/dev/null 2>&1; then
    echo "    reusing bucket"
else
    "$OCI_BIN" os bucket create --compartment-id "$TENANCY" --name "$BUCKET" >/dev/null
    echo "    created bucket $BUCKET (private)"
fi

echo "==> dynamic group + policy (instance principals)"
DG_ID="$(json_get iam dynamic-group list --all \
    --query "data[?name=='blink-backup-dg'].id | [0]")"
DG_RULE="instance.compartment.id = '$TENANCY'"
if [[ -z "$DG_ID" ]]; then
    DG_ID="$(json_get iam dynamic-group create \
        --name blink-backup-dg \
        --description "blink-to-telegram VM backup to object storage" \
        --matching-rule "$DG_RULE" \
        --query 'data.id')"
    echo "    created dynamic group"
else
    "$OCI_BIN" iam dynamic-group update --dynamic-group-id "$DG_ID" \
        --matching-rule "$DG_RULE" >/dev/null
    echo "    reusing dynamic group (rule refreshed)"
fi

if ! "$OCI_BIN" iam policy list --compartment-id "$TENANCY" \
        --query "data[?name=='blink-backup-policy']" --raw-output 2>/dev/null | grep -q blink-backup-policy; then
    "$OCI_BIN" iam policy create \
        --compartment-id "$TENANCY" \
        --name blink-backup-policy \
        --description "Allow blink-backup-dg to manage objects in $BUCKET only" \
        --statements "[\"Allow dynamic-group blink-backup-dg to manage objects in tenancy where target.bucket.name = '$BUCKET'\"]" \
        >/dev/null
    echo "    created policy"
else
    echo "    reusing policy"
fi
echo "    NOTE: IAM changes can take up to an hour to propagate."

# ---------------------------------------------------------------------------
# optional bootstrap
# ---------------------------------------------------------------------------
if [[ "$BOOTSTRAP" -eq 1 ]]; then
    echo "==> bootstrapping the VM"
    rsync -a --exclude .venv --exclude .git --exclude data --exclude .env \
        --exclude '*.egg-info' --exclude __pycache__ --exclude '*.pyc' \
        -e "ssh -i $SSH_KEY" "$REPO_DIR/" "ubuntu@$PUBLIC_IP:~/blink-to-telegram/"
    ssh -i "$SSH_KEY" "ubuntu@$PUBLIC_IP" 'sudo ~/blink-to-telegram/scripts/install.sh'

    # Install and disable the cron job: it must not run before the login file
    # and (if applicable) the restored state database are in place.
    ssh -i "$SSH_KEY" "ubuntu@$PUBLIC_IP" '
        sudo mv /etc/cron.d/blink-forwarder /etc/cron.d/blink-forwarder.disabled 2>/dev/null || true
        sudo apt-get install -y -qq sqlite3 >/dev/null
        sudo python3 -m venv /opt/oci-cli-venv >/dev/null 2>&1 || true
        sudo /opt/oci-cli-venv/bin/pip install --quiet oci-cli
        sudo ln -sf /opt/oci-cli-venv/bin/oci /usr/local/bin/oci
        sudo tee /usr/local/sbin/blink-backup.sh > /dev/null <<"EOS"
#!/bin/bash
set -euo pipefail
DATA_DIR=/var/lib/blink-to-telegram
BUCKET=blink-forwarder-state
OCI=/usr/local/bin/oci
sqlite3 "$DATA_DIR/state.db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
for f in state.db blink_login.json; do
    [ -f "$DATA_DIR/$f" ] && "$OCI" os object put --bucket-name "$BUCKET" \
        --name "$f" --file "$DATA_DIR/$f" --force --auth instance_principal >/dev/null
done
"$OCI" os object put --bucket-name "$BUCKET" --name env \
    --file /etc/blink-to-telegram/env --force --auth instance_principal >/dev/null
echo "$(date -u +%FT%TZ) backed up: state.db blink_login.json env"
EOS
        sudo chmod 750 /usr/local/sbin/blink-backup.sh
        sudo tee /etc/cron.d/blink-backup > /dev/null <<"EOS"
17 9 * * * root /usr/local/sbin/blink-backup.sh >> /var/log/blink-backup.log 2>&1
EOS
        sudo chmod 644 /etc/cron.d/blink-backup
    '

    # copy local .env values (token, chat id, tz) without printing them
    if [[ -f "$REPO_DIR/.env" ]]; then
        grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_SILENT|TZ)=' "$REPO_DIR/.env" \
            | ssh -i "$SSH_KEY" "ubuntu@$PUBLIC_IP" '
                sudo tee /tmp/env.override >/dev/null
                sudo python3 - <<"EOS"
over = {}
for line in open("/tmp/env.override"):
    line = line.strip()
    if "=" in line:
        k, v = line.split("=", 1)
        over[k] = v
lines = open("/etc/blink-to-telegram/env").read().splitlines()
out, used = [], set()
for line in lines:
    k = line.split("=", 1)[0].strip() if ("=" in line and not line.lstrip().startswith("#")) else None
    if k in over:
        out.append(f"{k}={over[k]}")
        used.add(k)
    else:
        out.append(line)
for k, v in over.items():
    if k not in used:
        out.append(f"{k}={v}")
open("/etc/blink-to-telegram/env", "w").write("\n".join(out) + "\n")
EOS
                sudo chown blink:blink /etc/blink-to-telegram/env
                sudo chmod 600 /etc/blink-to-telegram/env
                sudo rm /tmp/env.override
            '
        echo "    copied Telegram settings from local .env"
    fi
fi

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
cat <<EOF

Provisioning complete. State saved in $STATE_FILE

  public IP:  $PUBLIC_IP
  ssh:        ssh -i $SSH_KEY ubuntu@$PUBLIC_IP

Remaining steps (mostly yours):
EOF

if [[ "$BOOTSTRAP" -eq 1 ]]; then
cat <<EOF
  1. If you have local history, copy it to the VM FIRST (prevents re-uploads):
       .venv/bin/python -c "import sqlite3;sqlite3.connect('data/state.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
       scp -i $SSH_KEY data/state.db ubuntu@$PUBLIC_IP:/tmp/state.db
       ssh -i $SSH_KEY ubuntu@$PUBLIC_IP 'sudo install -o blink -g blink -m 600 /tmp/state.db /var/lib/blink-to-telegram/state.db && sudo rm /tmp/state.db'
  2. Blink login (interactive, handles 2FA):
       ssh -t -i $SSH_KEY ubuntu@$PUBLIC_IP "sudo -u blink blink-login"
  3. Verify:
       ssh -i $SSH_KEY ubuntu@$PUBLIC_IP "sudo -u blink blink-forwarder --check"
       ssh -i $SSH_KEY ubuntu@$PUBLIC_IP "sudo -u blink blink-forwarder --dry-run"
  4. Enable the schedule:
       ssh -i $SSH_KEY ubuntu@$PUBLIC_IP 'sudo mv /etc/cron.d/blink-forwarder.disabled /etc/cron.d/blink-forwarder'
  5. The daily state backup cron starts working once IAM propagates (up to 1h).
EOF
else
cat <<EOF
  1. sync this repo to the VM, then run: sudo ~/blink-to-telegram/scripts/install.sh
  2. copy state.db history (see deploy/oci/README.md) BEFORE enabling the cron job
  3. ssh -t -i $SSH_KEY ubuntu@$PUBLIC_IP "sudo -u blink blink-login"
  4. sudo -u blink blink-forwarder --check && sudo -u blink blink-forwarder --dry-run
  5. enable /etc/cron.d/blink-forwarder
EOF
fi
