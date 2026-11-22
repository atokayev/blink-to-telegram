# Security Policy

## Reporting a vulnerability

Use [GitHub's private vulnerability reporting](https://github.com/atokayev/blink-to-telegram/security/advisories/new)
rather than a public issue. Please include steps to reproduce and, if possible,
a suggested fix. You can expect a response within a few days.

## Scope

In scope:

- Anything that causes duplicate video posting, missed clips, or data loss in
  `state.db`.
- Credential exposure: bot tokens, Blink session data, or config values
  leaking through logs, alerts, or the archive.
- The OCI provisioning/restore scripts (`scripts/oci_*.sh`): overly broad IAM
  grants, missing hardening, injection via filenames.

Out of scope:

- Vulnerabilities in `blinkpy`, Blink's API, or the Telegram Bot API — report
  those upstream. This project is an unofficial client and is not affiliated
  with Blink or Telegram.

## Handling secrets

- The bot token lives only in the config file (mode 600) and is never written
  to logs or the Telegram archive.
- The Blink login file contains OAuth tokens, never the account password, and
  is stored mode 600.
- If you suspect a leaked token: rotate it with @BotFather and re-run
  `blink-login`. Daily backups contain these files — the backup bucket must
  stay private (the bundled IAM policy allows writes to that bucket only).
