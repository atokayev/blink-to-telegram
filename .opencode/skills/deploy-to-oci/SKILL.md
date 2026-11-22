---
name: Deploy blink-to-telegram to OCI
description: >
  Provision, deploy, verify, back up, and restore the Blink local-storage to
  Telegram forwarder on an Oracle Cloud Always Free VM using the OCI CLI.
  Covers instance launch with capacity retries, history migration without
  duplicates, Blink login, cron enablement, and disaster recovery.
---

Deploy the blink-to-telegram forwarder to Oracle Cloud Always Free. Work from
this repository's root. The repo's `scripts/oci_provision.sh` does the heavy
lifting; your job is to run it, guard the ordering rules below, and verify.

## Hard rules (violations cause duplicate video floods or broken auth)

1. **Never enable the forwarder cron job before the history `state.db` is
   restored and Blink is logged in.** The provisioning script leaves
   `/etc/cron.d/blink-forwarder.disabled`; only re-enable it after steps
   "history" and "login" below are confirmed.
2. **Never print or echo the Telegram bot token or Blink credentials.** Copy
   them via stdin pipes or files that are deleted afterwards. Redact any
   accidental output.
3. **The Blink login (`blink-login`) is always done by the human**: it prompts
   for the account password and a 2FA code. Give them the exact command
   (`ssh -t -i <key> ubuntu@<ip> "sudo -u blink blink-login"`) and wait for
   confirmation. Never try to automate credentials.
4. Every successful send is marked in `state.db` only after Telegram accepts
   the video. A killed or recreated process is safe to resume.

## Pre-flight

- OCI CLI configured? `oci os ns get` must return the object-storage
  namespace. If it returns 401 immediately after the user uploaded an API
  key, wait a few minutes: IAM propagation lags and can flap (some requests
  200, some 401) — retry, do not reconfigure.
- Home region: `oci iam region-subscription list` — Always Free resources must
  live in the home region; warn if the config region differs.
- `jq`-style `--query` expressions run on the `data` wrapper; a successful
  call with an empty result prints nothing. Check exit codes and use
  `--output json` when debugging.

## Provision

```
scripts/oci_provision.sh --bootstrap
```

This creates (idempotently, all free tier): VCN `blink-vcn`, internet
gateway, default route, SSH-only security list, subnet `blink-public`, a
`VM.Standard.E2.1.Micro` with Ubuntu 24.04 named `blink-forwarder` (SSH key at
`~/.oci/blink_oci`), bucket `blink-forwarder-state`, dynamic group
`blink-backup-dg` with rule `instance.compartment.id = '<tenancy>'`, policy
`blink-backup-policy`, and — with `--bootstrap` — syncs the repo to the VM,
runs `scripts/install.sh`, disables cron, and installs the daily backup
script plus `/etc/cron.d/blink-backup`.

Gotchas:

- "Out of host capacity" on launch: the script retries across all
  availability domains (6 rounds × 30 s). If it still fails, wait and re-run;
  the script is idempotent. Upgrading the tenancy to Pay As You Go (Always
  Free resources stay free) removes capacity errors.
- `VM.Standard.E2.1.Micro` can only be created in one availability domain per
  region.
- Ubuntu has no apt package for the OCI CLI; the script installs it into
  `/opt/oci-cli-venv` on the VM and symlinks `/usr/local/bin/oci`.

## History migration (mandatory when clips were already sent elsewhere)

1. On the source machine, stop any running forwarder and checkpoint the WAL:
   `python3 -c "import sqlite3; sqlite3.connect('data/state.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"`
2. `scp data/state.db` to the VM and install it:
   `sudo install -o blink -g blink -m 600 /tmp/state.db /var/lib/blink-to-telegram/state.db`
3. Verify: `sudo sqlite3 /var/lib/blink-to-telegram/state.db "SELECT status, COUNT(*) FROM clips GROUP BY status;"`
   must match the source counts exactly.

## Login and verification

1. Ask the user to run: `ssh -t -i ~/.oci/blink_oci ubuntu@<ip> "sudo -u blink blink-login"`
2. Then verify yourself:
   `ssh -i ~/.oci/blink_oci ubuntu@<ip> "sudo -u blink blink-forwarder --check"`
   All items must pass; `can_post_messages=False` on a `supergroup` is fine
   (that field only exists for channels; group admins post anyway).
3. Dry run: `... blink-forwarder --dry-run --max-clips 999`. The count must
   equal (total clips on USB) − (rows already marked sent in `state.db`).
   If it lists clips that were already posted elsewhere, the history was not
   migrated — stop and fix before enabling cron.
4. Only then enable the schedule:
   `sudo mv /etc/cron.d/blink-forwarder.disabled /etc/cron.d/blink-forwarder`

## Backups and disaster recovery

- Daily 09:17 UTC cron on the VM uploads `state.db`, `blink_login.json`, and
  `env` to bucket `blink-forwarder-state` via instance principals.
- Matching-rule changes and new IAM policies can take up to an hour to
  propagate; retry the backup script every 5 minutes rather than rewriting
  IAM. Only `instance.compartment.id` and `instance.id` are valid
  dynamic-group variables for compute instances — `resource.*` does not match
  instances.
- To restore on a fresh VM: `sudo ~/blink-to-telegram/scripts/restore_from_oci.sh`
  (downloads state, login, and env, disables cron during restore, re-enables
  after). See the "Disaster recovery" section of `README.md`.

## Useful diagnostics

- Forwarder logs on the VM: `sudo tail -f /var/log/blink-forwarder.log`
- Counters: `sudo -u blink blink-forwarder --stats`
- Telegram raw chat/member diagnostics: `python scripts/telegram_diag.py`
- Stop a runaway upload: `pkill -f blink_forwarder` (kill the python process,
  not just a pipeline shell — orphans keep sending).
