# followbot

A personal Telegram bot that follows X and Instagram accounts for you, from your own logged-in accounts, without leaving Telegram. It answers only your Telegram ID.

> ⚠️ Automating follows through X's and Instagram's private web APIs breaks both platforms' terms. Your account can be rate-limited, challenged, or banned. Always test on a throwaway account first (see [execution-phase.md](execution-phase.md), phase 3).

## The docs

| File | What's in it |
|---|---|
| [README.md](README.md) | This overview |
| [tech-stack.md](tech-stack.md) | What it's built with, how the pieces fit together, and why |
| [execution-phase.md](execution-phase.md) | Every remaining step to go live, and the points where **you** need to act (marked 🙋) |
| [my-use-flow.md](my-use-flow.md) | Day-to-day use: connecting accounts, following, alerts, refreshing cookies |

It runs **on this machine** in Docker. X and Instagram traffic goes out through Surfshark's Ashburn servers (one gluetun container per server, so each account has its own IP); Telegram uses your normal connection.

## What it does

- **Connect accounts with cookies.** You tap **Add account**, choose X or Instagram, then send the JSON export from your cookie extension. The bot deletes your message, checks the session through the VPN, and stores the cookies encrypted (AES-256-GCM). It never uses passwords or the paid official APIs.
- **Follow one account or a list.** `/follow` takes handles, `@handles`, profile links, or a `.txt`/`.csv` file. You see a preview first (duplicates, invalid entries, ETA, daily-cap warning), then press **Start**. Progress shows up live, and a final report lists anything that failed.
- **Pacing.** Each account gets at most **13 follows per 10 minutes** (single follows count too), with random 35–65 s gaps (about 12 in a typical 10 minutes), plus a daily cap (X 200, Instagram 100). Accounts you already follow are skipped without spending budget.
- **Safety stops:**
  - **Rate limited:** the account pauses for hours and then resumes on its own.
  - **Session expired:** the queue is held until you reconnect.
  - **Challenge or locked account:** the queue is held until you fix it in the app and send `/resume`.
  - **VPN down:** follows are retried, and nothing is ever sent outside the VPN.
- **VPN.** Each account gets its own Ashburn Surfshark server, and so its own exit IP, and keeps it. Five servers run by default (`main`, `ash2` … `ash5`); a sixth account would share the least-used one. Only X and Instagram traffic goes through the VPN.

## Where things are

```
followbot/          bot source (see tech-stack.md for the module map)
tests/              offline test suite (50 tests)
docs/               these documents
Dockerfile          bot image (non-root, read-only filesystem)
docker-compose.yml  bot + gluetun VPN container(s)
secrets/            your cookie exports (git-ignored, chmod 700); imported at startup if named in .env.local
dc                  wrapper for `docker compose`: loads .env.local and runs the bot as your user
.env.example        template for every secret and setting (your copy is .env.local)
```

## Secrets

Every secret lives in **`.env.local` on this machine** (`chmod 600`, git-ignored) and never in the code, the docs, or a chat with Claude. Your account cookies **don't go inside `.env.local`**. Either put each export in `secrets/` and name the file in `.env.local` (`X_COOKIES_FILE=x.cookies.json`), or send it to the bot with `/connect`. Either way, the bot stores the cookies encrypted with `ENCRYPTION_KEY`. Each secret and where to get it is listed in [execution-phase.md](execution-phase.md#phase-1-fill-in-envlocal-and-prepare-test-accounts-).

## Quick commands

```bash
./dc up -d --build
```
```bash
./dc logs -f bot
```
```bash
docker run --rm -v "$PWD":/app -w /app followbot-dev python -m pytest -q -p no:cacheprovider
```
