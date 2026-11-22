# Deploying to Oracle Cloud Always Free

This guide covers the full OCI deployment. If an AI coding agent (OpenCode,
Claude Code, …) has access to your machine and OCI CLI, it can do all of this
for you — load the bundled `deploy-to-oci` skill (`.opencode/skills/`) or just
point it at `scripts/oci_provision.sh`.

## What you get

| Resource | Always Free allotment |
| --- | --- |
| `VM.Standard.E2.1.Micro` (AMD, 1 GB RAM) | 2 instances, no hours cap |
| Block volume | 50 GB boot volume (200 GB total) |
| Object Storage (backups) | 20 GB — one daily `state.db` backup is ~200 KB |
| Outbound transfer | 10 TB/month (this app uses ~1 GB) |

Total cost: **$0**. One caveat: Oracle may reclaim *idle* Always Free
instances; the daily backup keeps your `state.db` safe either way.

## Prerequisites

1. OCI CLI installed and configured (`oci setup config`), with the API key
   uploaded in the console under *My profile → API keys*.
2. The config region must be your **home region** (Always Free resources
   cannot live elsewhere).

## Fast path (one command)

```bash
scripts/oci_provision.sh --bootstrap
```

Creates everything idempotently:

- Network: VCN, internet gateway, default route, public subnet, SSH-only
  security list (port 22 only — the app needs no inbound ports).
- Compute: `VM.Standard.E2.1.Micro` + Ubuntu 24.04, SSH key `~/.oci/blink_oci`,
  public IP, with a retry loop for "out of host capacity".
- Backups: private bucket `blink-forwarder-state`, dynamic group +
  policy for instance principals (no API keys stored on the VM).
- With `--bootstrap`: syncs this repo to the VM, runs `scripts/install.sh`,
  **disables the cron job** until you finish the steps below, copies
  `TELEGRAM_*`/`TZ` values from your local `.env`, and installs the daily
  backup cron.

State (OCIDs, IP) is saved to `~/.oci/blink_stack.env`. Re-running the script
reuses whatever already exists.

## Finish by hand (these are deliberately interactive)

1. **Migrate history first** if clips were already sent from anywhere else —
   otherwise the fresh VM re-posts the entire backlog:

   ```bash
   # on the machine with the existing state.db
   python3 -c "import sqlite3; sqlite3.connect('data/state.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
   scp data/state.db ubuntu@<ip>:/tmp/state.db
   ssh ubuntu@<ip> 'sudo install -o blink -g blink -m 600 /tmp/state.db /var/lib/blink-to-telegram/state.db && sudo rm /tmp/state.db'
   # verify: counts must match the source
   ssh ubuntu@<ip> 'sudo sqlite3 /var/lib/blink-to-telegram/state.db "SELECT status, COUNT(*) FROM clips GROUP BY status;"'
   ```

2. **Blink login** (prompts for password + 2FA; cannot be automated):

   ```bash
   ssh -t -i ~/.oci/blink_oci ubuntu@<ip> "sudo -u blink blink-login"
   ```

3. **Verify before enabling the schedule:**

   ```bash
   ssh -i ~/.oci/blink_oci ubuntu@<ip> "sudo -u blink blink-forwarder --check"
   ssh -i ~/.oci/blink_oci ubuntu@<ip> "sudo -u blink blink-forwarder --dry-run"
   ```

   The dry run must list exactly *new* clips — none of the ones recorded in
   the migrated `state.db`. If it does, ordering and dedup are proven.

4. **Enable the cron job:**

   ```bash
   ssh -i ~/.oci/blink_oci ubuntu@<ip> \
     'sudo mv /etc/cron.d/blink-forwarder.disabled /etc/cron.d/blink-forwarder'
   ```

## Manual path (no script)

Follow `README.md` sections 2–7 in the web console, or use
`scripts/oci_provision.sh` step by step. The script is a readable bash
translation of every console step.

## IAM notes that bite everyone

- Dynamic-group matching rules for compute instances only support
  `instance.compartment.id` and `instance.id`. The `resource.*` variables do
  **not** match compute instances.
- After creating/updating a dynamic group or policy, allow up to **one hour**
  for IAM changes to propagate (small uploads may succeed while others 401
  during propagation — retry, don't reconfigure).
- The bundled policy is deliberately narrow: it grants object access only in
  the `blink-forwarder-state` bucket.
- A Pay As You Go upgrade (Always Free resources remain free) removes both
  "out of host capacity" errors and idle reclamation, if that tradeoff
  (card on file) is acceptable to you.

## Disaster recovery

If the VM is lost or reclaimed:

```bash
scripts/oci_provision.sh --bootstrap          # recreate everything
ssh -i ~/.oci/blink_oci ubuntu@<new-ip> 'sudo ~/blink-to-telegram/scripts/restore_from_oci.sh'
```

The restore pulls the last `state.db`, `blink_login.json`, and `env` from the
bucket — no re-flood of old clips, no 2FA re-login. The daily backup cron is
installed by the provision script.

Verify backups any time:

```bash
oci os object list --bucket-name blink-forwarder-state --auth instance_principal
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401 NotAuthenticated` right after adding an API key | Wait a few minutes; IAM propagation flaps. Retry `oci os ns get`. |
| `Out of host capacity` | The script retries; otherwise wait, try another AD, or upgrade to PAYG. |
| Backup `BucketNotFound` | IAM propagation (up to 1h) or the dynamic-group rule was edited to use `resource.*`. Use `instance.compartment.id`. |
| Clips re-uploading on a fresh VM | History was not migrated; see step 1. Stop cron, restore `state.db`, verify with `--dry-run`. |
| `can_post_messages=False` | Harmless if the chat is a `supergroup` (that field is channels-only). See the main README troubleshooting. |
