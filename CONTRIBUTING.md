# Contributing

Thanks for considering a contribution! This project is intentionally small and
opinionated, so most changes are best discussed in an issue first.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # fill in a *test* bot token; never your real one
scripts/local_test.sh       # compile + unit tests + CLI checks, no credentials
```

## Ground rules

- **Never commit secrets.** `.env`, `data/`, and `data/blink_login.json` are
  gitignored. CI runs gitleaks on every push and PR, and push protection is
  enabled — a leaked token will block the push and then rotate it.
- **Test with a disposable Blink session and Telegram chat.** Real camera
  footage and real channels make review impossible.
- **Keep the dependency pin honest.** The code leans on `blinkpy`
  local-storage internals; if you bump the pin, say so in the PR and explain
  what changed.

## Code style

- Python 3.10+ standard library plus `aiohttp`/`blinkpy`/`python-dotenv` only —
  no new runtime dependencies without discussion.
- `asyncio` for all I/O; the SQLite wrapper stays synchronous (single
  threaded process).
- Every new behaviour gets a unit test; the fake Blink/Telegram harness in
  `tests/test_forwarder.py` is the pattern to follow.

## Pull requests

1. Fork, branch, make the change.
2. `.venv/bin/python -m unittest discover -s tests -v` must pass.
3. `gitleaks git .` should report no leaks (CI will check anyway).
4. Keep PRs focused; one behaviour per PR.
