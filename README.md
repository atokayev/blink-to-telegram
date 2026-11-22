# Blink → Telegram Forwarder

Forward every motion clip from a Blink Sync Module's USB drive to a Telegram
channel, running unattended on an Oracle Cloud Always Free VM.

The forwarder runs from cron every 5 minutes, asks Blink for the clips currently
on the Sync Module, downloads any it has not seen before, posts them with
Telegram's `sendVideo`, and records each clip in SQLite so it is never posted
twice. Originals are kept in a local archive that is pruned after 90 days by
default.

```
Blink cameras ──> Sync Module 2 ──> USB drive
                        │
                        │  Blink cloud API (manifest + clip download)
                        ▼
             Oracle Cloud VM (cron)
                        │
                        ├── SQLite dedup state
                        ├── local archive/
                        ▼
                 Telegram channel
```

## Why the VM talks to Blink's API

A cloud VM cannot see a USB drive in your house. Blink does not expose the Sync
Module's USB filesystem on the LAN either. The official app works by asking
Blink's cloud to build a manifest of the clips on the module, then asking the
module to upload a chosen clip to the cloud for download. This project uses the
same flow through [`blinkpy`](https://github.com/fronzbot/blinkpy), so no extra
hardware is needed at home.

This works best on an account **without an active Blink subscription**: the Sync
Module then writes each clip to USB as it is recorded. With a subscription,
Blink may only batch-backup clips to USB once a day under a different folder.
The API manifest is still what this project reads.

## Requirements

- Blink Sync Module 2 with a USB drive, or Sync Module XR with a microSD card,
  with **Local Storage active** in the Blink app.
- Blink account credentials and access to the 2FA code for the one-time login.
- A Telegram bot and a channel where the bot is an administrator.
- An Oracle Cloud Always Free Ubuntu 22.04/24.04 instance (AMD micro or ARM
  Ampere; 1 GB RAM is enough).
- Python 3.10+ (Ubuntu 22.04 ships 3.10, 24.04 ships 3.12).

No inbound ports need to be opened on the Oracle instance. The VM only makes
outbound HTTPS requests to Blink and Telegram.

## 1. Create the Telegram bot and channel

1. Message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the
   token (looks like `123456789:AA...`).
2. Create a channel (or use an existing one).
3. Add the bot to the channel as an **administrator** with the **Post Messages**
   permission. A bot cannot post to a channel it is not an admin of.
4. Get the channel id:
   - Public channel: the `@channelname` handle works as `TELEGRAM_CHAT_ID`.
   - Private channel: post any message in the channel, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for
     `"chat":{"id":-100...}`. The numeric id starts with `-100`.

Test it:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getChat?chat_id=<CHAT_ID>"
```

## 2. Create the Oracle Cloud instance

In the Oracle Cloud console:

1. **Compute → Instances → Create instance.**
2. Image: **Ubuntu 24.04** (or 22.04).
3. Shape: **VM.Standard.E2.1.Micro** (Always Free AMD) or
   **VM.Standard.A1.Flex** (Always Free ARM).
4. Add your SSH public key and create the instance.
5. Leave the default security list alone. No ingress rules are required.

Connect:

```bash
ssh ubuntu@<public-ip>
```

## 3. Install

Copy this repository to the VM (git clone, `scp`, or paste it), then:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <your-repo-url> ~/blink-to-telegram
sudo ~/blink-to-telegram/scripts/install.sh
```

The installer:

- creates the `blink` system user;
- installs the app under `/opt/blink-to-telegram` in a virtualenv;
- creates `/etc/blink-to-telegram/env` (mode 600) and
  `/var/lib/blink-to-telegram` for state and the archive;
- installs `/usr/local/bin/blink-forwarder` and `blink-login`;
- installs a cron job every 5 minutes (or a systemd timer with `--systemd`);
- sets up log rotation for `/var/log/blink-forwarder.log`.

## 4. Configure

```bash
sudo nano /etc/blink-to-telegram/env
```

At minimum set:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=-1001234567890
TZ=America/New_York
```

See the full table in [Configuration](#configuration).

## 5. Authenticate Blink (one time)

```bash
sudo -u blink blink-login
```

Enter your Blink email/password and the 2FA code if Blink asks for one. The
script saves a refresh token to
`/var/lib/blink-to-telegram/blink_login.json` with mode 600. **The account
password is not stored.** It also prints each Sync Module and whether Local
Storage is active.

> The login must run on the VM (or the token file must be copied there) because
> the refresh token is tied to a generated hardware id. Re-run `blink-login` if
> the refresh token is ever revoked.

## 6. Validate and test

```bash
# Checks the config, bot token, channel access, and login file
sudo -u blink blink-forwarder --check

# Lists clips that would be sent; downloads nothing and posts nothing
sudo -u blink blink-forwarder --dry-run

# First real run (capped at MAX_CLIPS_PER_RUN, default 50)
sudo -u blink blink-forwarder

# Follow the scheduled runs
tail -f /var/log/blink-forwarder.log
```

The first run may have a large backlog. The per-run cap drains it 50 clips at a
time; later cron runs continue automatically. At ~4 seconds per upload, 50
clips takes about 3–4 minutes.

## Local test before deploying to OCI

You can run the full pipeline on your laptop first. Everything local lives in
`data/`, which is gitignored.

```bash
cd blink-to-telegram
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TZ
```

Run the unit tests (no credentials needed):

```bash
.venv/bin/python -m unittest discover -s tests -v
# or, for compile + tests + CLI check in one step:
scripts/local_test.sh
```

Validate Telegram first. The Blink login file will still be reported missing,
which is expected at this point:

```bash
.venv/bin/python -m blink_forwarder --check
```

Log in interactively. This writes `data/blink_login.json` (mode 600) and prints
each Sync Module's local-storage status:

```bash
.venv/bin/python scripts/blink_login.py
```

Then check again, preview, and send exactly one clip:

```bash
.venv/bin/python -m blink_forwarder --check
.venv/bin/python -m blink_forwarder --dry-run
.venv/bin/python -m blink_forwarder --max-clips 1
```

Confirm the video appears in the channel, then run without the cap:

```bash
.venv/bin/python -m blink_forwarder
```

To repeat the test, stop the job and delete `data/state.db` so the same clips
are eligible again. Delete `data/archive/` too if you do not want the local
copies. For production, run `blink-login` on the VM rather than copying the
token file, because Blink ties the session to a generated hardware id.

## 7. Schedule

The default cron entry is `/etc/cron.d/blink-forwarder`:

```cron
*/5 * * * * blink /usr/bin/flock -n /var/lib/blink-to-telegram/forwarder.lock \
  /usr/local/bin/blink-forwarder >> /var/log/blink-forwarder.log 2>&1
```

`flock` prevents overlapping runs. To change the interval, edit that file (or
the systemd timer if installed with `--systemd`).

If you prefer systemd:

```bash
sudo ~/blink-to-telegram/scripts/install.sh --systemd
systemctl list-timers blink-forwarder.timer
journalctl -u blink-forwarder.service -f
```

## Configuration

All settings live in `/etc/blink-to-telegram/env` (or a local `.env` for
development).

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | required | Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | required | Channel numeric id (`-100...`) or `@public_name`. |
| `TELEGRAM_SILENT` | `false` | Post videos without a notification sound. |
| `BLINK_LOGIN_FILE` | `$BLINK_DATA_DIR/blink_login.json` | Saved Blink session. |
| `BLINK_DATA_DIR` | `data` | Base directory for state, archive, temp files. |
| `BLINK_DB` | `$BLINK_DATA_DIR/state.db` | SQLite dedup database. |
| `BLINK_ARCHIVE` | `$BLINK_DATA_DIR/archive` | Where delivered MP4s are kept. |
| `BLINK_TMP` | `$BLINK_DATA_DIR/tmp` | Temporary downloads. |
| `TZ` | `UTC` | Timezone used in captions (archive folders are UTC). |
| `MAX_FILE_MB` | `49` | Skip clips larger than this (bot API limit is 50 MB). |
| `MAX_CLIPS_PER_RUN` | `50` | Per-run send cap; the backlog drains over later runs. |
| `SEND_DELAY_SECONDS` | `4` | Delay between uploads (channel rate limit). |
| `RETENTION_DAYS` | `90` | Delete archived MP4s older than this; `0` keeps forever. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `DRY_RUN` | `false` | Set `true` to list clips without sending. |

## How deduplication works

Each clip gets a stable source key:

```
<sync serial>|<camera>|<UTC timestamp>|<reported size>
```

The manifest and clip ids are deliberately excluded because Blink can rebuild
them. Before downloading, the forwarder checks the key in SQLite. After
downloading it also computes a SHA-256 of the file and refuses to post content
that was already sent, which catches duplicate clips across manifests.

Delivery is **at-least-once**: if the process is killed after Telegram accepts a
video but before the SQLite commit, that one clip can be posted again. The
reverse (losing a clip) is avoided by only marking a clip after Telegram returns
a message id.

## Limitations and operational notes

- **Polling, not real time.** Clips arrive within one cron interval (5 minutes)
  after Blink finishes writing them to the USB drive.
- **Backlog order.** Clips are always sent oldest-first, so a large backlog
  drains chronologically instead of in whatever order Blink returns them.
- **Blink cloud dependency.** The download flow asks the Sync Module to upload a
  clip to Blink's cloud and then downloads it, so clips transit Blink's servers
  even though they were recorded locally. The Sync Module must be online.
- **50 MB Bot API ceiling.** Larger clips are skipped and reported once a day.
- **The forwarder never deletes from the USB drive.** A Sync Module 2 accepts up
  to 256 GB. If the drive fills, Blink stops saving new clips, so clean old
  files from the Blink app periodically (or extend the project with a delete
  option).
- **Unofficial API.** `blinkpy` is not supported by Blink. The dependency is
  pinned to the version this code was tested against; test upgrades before
  rolling them out.
- **Re-sending after a USB format.** If you reformat the USB drive and want new
  files with matching camera/timestamp/size to be sent again, stop the job and
  delete `/var/lib/blink-to-telegram/state.db`. The archive is unaffected.


## Troubleshooting

**`Blink forwarder needs a login` alert / exit code 2**

The saved refresh token expired or Blink asked for 2FA. Run:

```bash
sudo -u blink blink-login
```

Alerts are throttled to one per 12 hours, so a broken login will not spam the
channel every 5 minutes.

**`local storage is not active`**

In the Blink app open the Sync Module → **Local Storage**, insert and format the
USB drive, and make sure Local Storage is enabled. The Sync Module must be
online. Re-run `blink-login` to see the status.

**No clips found, but the USB drive has videos**

- The Sync Module may be offline or busy; try again in a few minutes.
- With an active subscription Blink may use a daily clip-backup flow; the API
  manifest still lists local clips, but they may appear only once a day.
- Check the log at `/var/log/blink-forwarder.log` for manifest errors.

**Telegram 403 / "not enough rights"**

The bot must be an administrator of the channel with the Post Messages
permission. For a private channel use the numeric `-100...` id, not the invite
link.

**Telegram 429 (rate limit)**

The channel limit is roughly 20 messages per minute. Increase
`SEND_DELAY_SECONDS`, lower `MAX_CLIPS_PER_RUN`, or let the forwarder retry — it
honours `retry_after` automatically.

**Clip too large / 50 MB limit**

Bot API uploads are capped at 50 MB. The clip is left on the USB drive and
marked skipped; a throttled alert is posted. To forward larger files you would
need a self-hosted local Bot API server (not covered here) or manual handling.

If you later raise `MAX_FILE_MB`, previously skipped clips are not retried
automatically (they are recorded as skipped). Clear just those rows, then run
again:

```bash
sudo -u blink sqlite3 /var/lib/blink-to-telegram/state.db \
  "DELETE FROM clips WHERE status='skipped' AND reason LIKE 'too_large%';"
```

**`can_post_messages=False` even though the bot is an administrator**

`can_post_messages` is a **channels-only** field. If `--check` reports the chat
as `supergroup` (a group), that field does not exist for it, so `False` is
expected and not an error. Groups and channels both work as targets; what
matters is that the bot is an administrator:

- Group (`supergroup`): the bot posts as an administrator. Prove it end to end
  with `python scripts/telegram_diag.py --send-test`.
- Channel (`channel`): the bot must have the **Post Messages** admin permission
  (Channel → Administrators → bot → enable Post Messages).

If you meant to use a channel but `--check` shows `supergroup`, you added the
bot to a group (or a channel's linked discussion group). Create the channel via
Telegram → New Channel → Private, add the bot as an administrator with Post
Messages, then update `TELEGRAM_CHAT_ID`.

**`--check` says the data directory is not writable**

Run the command as the `blink` user (`sudo -u blink ...`) and confirm
`/var/lib/blink-to-telegram` is owned by `blink:blink`.

**Oracle reclaims idle Always Free instances**

Oracle may reclaim Always Free compute instances that stay idle for long
periods. A 5-minute cron job usually keeps the instance active, but this is an
Oracle policy outside the project's control. Keep backups of
`/var/lib/blink-to-telegram` if the archive matters.

## Security notes

- `/etc/blink-to-telegram/env` and the Blink login file are mode 600 and owned
  by the unprivileged `blink` user.
- The Blink password is never written to disk; only OAuth tokens are stored.
- The bot token grants control of the bot. Treat it like a password and rotate
  it with @BotFather if leaked.
- Oracle boot volumes are encrypted at rest by default.
- `blinkpy` is an unofficial client. Blink can change its API at any time; the
  dependency is pinned to the version this code was written against.

## Manual operations

```bash
# Counters and last run time
sudo -u blink blink-forwarder --stats

# One-off run with a smaller cap
sudo -u blink blink-forwarder --max-clips 10

# Dry run
sudo -u blink blink-forwarder --dry-run

# Archive contents
find /var/lib/blink-to-telegram/archive -name '*.mp4' | tail

# Disable the cron job
sudo rm /etc/cron.d/blink-forwarder

# Or disable the systemd timer
sudo systemctl disable --now blink-forwarder.timer
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m blink_forwarder --check
```

The code is split into:

- `blink_forwarder/blink_source.py` — Blink login and local-storage manifest.
- `blink_forwarder/telegram.py` — Bot API uploads with retry/rate-limit handling.
- `blink_forwarder/db.py` — SQLite dedup and alert throttling.
- `blink_forwarder/forwarder.py` — the run loop.
- `scripts/blink_login.py` — interactive one-time login.
- `scripts/local_test.sh` — credential-free local smoke test.
- `scripts/install.sh` — Oracle/Ubuntu installer.
