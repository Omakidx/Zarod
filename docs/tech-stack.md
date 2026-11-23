# Tech stack

## At a glance

| Layer | Choice | Version | Why |
|---|---|---|---|
| Language | Python | 3.12 | Has the most mature libraries for Telegram and for private platform APIs |
| Telegram | aiogram | 3.32.0 | Async, with built-in state machines for multi-step flows like `/connect` and `/follow` |
| HTTP | httpx | 0.28.1 | Async, supports a per-client proxy, and lets tests swap in a mock transport |
| Browser fingerprint | curl_cffi | 0.16.3 | Used as httpx's transport for Instagram, so connections look like your browser's (not browser automation) |
| Storage | SQLite via aiosqlite | 0.22.1 | One file, no server, enough for one owner |
| Encryption | cryptography (AES-256-GCM) | 50.0.2 | Authenticated encryption of the stored cookies |
| VPN | gluetun + Surfshark (WireGuard) | gluetun v3.40.0 | One container per Ashburn server, each pinned to a server IP (`VPN_ENDPOINT_IP`) and exposing an HTTP proxy, so each account has its own fixed exit IP and only X/Instagram traffic uses the VPN |
| Runtime | Docker Compose, via the `./dc` wrapper, on this machine | — | One command starts the bot and the VPN together. `./dc` loads `.env.local` and runs the bot as your user |
| Tests | pytest + pytest-asyncio | 8.3.5 / 0.25.3 | Run fully offline against mocked platform responses |

There are no paid APIs, no browser automation (Playwright/Chrome), and no Redis or Celery queue.

## How it fits together

```
 You (Telegram) ──► Telegram API ◄──── bot container ───────────────┐
                                        │  aiogram handlers (bot.py) │
                                        │  queue worker  (worker.py) │
                                        │  SQLite /data/followbot.db │
                                        └───────────┬────────────────┘
                                                    │ X / Instagram requests only
                                                    ▼
              gluetun vpn, vpn2 … vpn5 (one per account, HTTP proxy :8888 each)    
                                                    │ WireGuard
                                                    ▼
                          Surfshark Ashburn servers (one per account, own exit IP)
                                                    │
                                                    ▼
                                         x.com  /  instagram.com
```

- Telegram traffic uses this machine's normal connection.
- The gluetun proxy has no published port, so only the bot can reach it.
- If the VPN is down, requests fail and get retried later. They are never sent directly.

## Module map

| File | Responsibility |
|---|---|
| `followbot/__main__.py` | Entry point. `run` (default), `keygen` (new encryption key), and `probe` (test cookies without Telegram; refuses to run without the VPN unless you pass `--direct`) |
| `followbot/config.py` | Loads and validates every setting from the environment |
| `followbot/crypto.py` | `CookieVault`: AES-GCM, with each ciphertext bound to `platform:username` |
| `followbot/cookies.py` | Parses cookie-extension JSON: checks domains, expiry, and required names |
| `followbot/handles.py` | Turns your input (handles, `@handles`, links) into clean, de-duplicated usernames |
| `followbot/importer.py` | Imports cookie exports named in `X_COOKIES_FILE` / `INSTAGRAM_COOKIES_FILE` from `secrets/` at startup. Each file is re-imported only when its content changes |
| `followbot/ratelimit.py` | The 13-per-10-minute window, the daily cap, random gaps, and the ETA estimate |
| `followbot/db.py` | SQLite tables: `accounts`, `jobs`, `tasks`, `actions` |
| `followbot/worker.py` | Background loop: runs each account's queue within its budget and pauses or holds it on errors |
| `followbot/bot.py` | Telegram commands, buttons, progress messages, and alerts |
| `followbot/platforms/x.py` | X adapter |
| `followbot/platforms/instagram.py` | Instagram adapter |
| `followbot/platforms/base.py` | The interface every adapter implements, plus the shared outcome and error types |

## How each platform is called

Both adapters send your cookies plus the CSRF header the website itself sends. They pick up any cookies the platform rotates and save them back, encrypted.

**X** (`https://x.com/i/api/1.1/…`, with the web app's public bearer token, `auth_token` + `ct0`)

| Purpose | Endpoint |
|---|---|
| Check the session | `GET account/multi/list.json` → your screen name, matched to the user ID in the `twid` cookie (fallback: `friendships/lookup.json?user_id=…`). `account/settings.json` was retired by X |
| Check the relationship | `GET friendships/lookup.json?screen_name=…` → `following` / `following_requested` |
| Follow | `POST friendships/create.json` with `screen_name` |

**Instagram** (`https://www.instagram.com/api/v1/…`, `x-ig-app-id` + `x-csrftoken` + `x-ig-www-claim`, cookies `sessionid` + `csrftoken` + `ds_user_id`). Requests go out through **curl_cffi** (`followbot/platforms/_impersonate.py`) with the TLS/HTTP-2 fingerprint of the browser in `BROWSER_USER_AGENT`. Instagram answers Python's own fingerprint with 429.

| Purpose | Endpoint |
|---|---|
| Check the session | `GET accounts/edit/web_form_data/` → your username (`users/{id}/info/` answers web sessions with 429) |
| Look up the profile | `GET users/web_profile_info/?username=…` → id, private, already following or requested. A 404 *page* means the profile is unavailable (same as "Sorry, this page isn't available" in the browser) |
| Follow | `POST /api/graphql`, the website's own `usePolarisFollowMutation` (`doc_id` in `followbot/platforms/instagram.py`), with the page tokens `fb_dtsg`, `lsd` and `av` read once from the homepage. `friendships/create/` now answers web sessions with an HTML page. If Instagram changes the mutation, capture it again with `tools/ig-capture` (a small Brave extension that copies the request shape without cookie or token values) |

> These are undocumented and can change without notice. Phase 3 of [execution-phase.md](execution-phase.md) checks them against the live platforms. If X starts requiring its `x-client-transaction-id` header, the plan is to switch the X adapter to the **twikit** library.

**What each outcome means**

| Outcome | Meaning |
|---|---|
| `followed` | You now follow the account |
| `requested` | Private or protected account; your follow request is pending |
| `already` | You already followed or requested it (no budget spent) |
| `not_found` | No such user |
| `unavailable` | Suspended, or blocked |
| `failed` | Network or VPN errors 3 times in a row |

**What happens when the platform pushes back**

| Error | What the bot does |
|---|---|
| X code 161, Instagram `feedback_required` | Pauses the account for 6 h (X) or 12 h (Instagram), then resumes on its own |
| HTTP 429 | Pauses the account for 15 min–1 h, then resumes on its own |
| 401, `login_required` | Marks the account *expired* and holds the queue until you `/connect` again |
| `challenge_required`, account locked | Marks the account *blocked* until you fix it and send `/resume` |
| Network or VPN errors, 5xx | Retries after 1, 2, then 3 minutes, then marks the task `failed` |

## Rate budget

Before every follow, the worker computes the earliest allowed time. It's the latest of these four:

- the end of the random 25–60 s gap after the previous follow;
- the moment the 10-minute window drops back under 13 follows;
- the moment the rolling 24 h window drops back under the daily cap;
- the end of any pause.

Only follow requests actually sent count toward the budget; lookups don't. Every limit can be changed in `.env.local` (`WINDOW_LIMIT`, `WINDOW_SECONDS`, `GAP_MIN_SECONDS`, `GAP_MAX_SECONDS`, `DAILY_LIMIT_X`, `DAILY_LIMIT_INSTAGRAM`).

## Security model

| Concern | How it's handled |
|---|---|
| Who can use the bot | Only `OWNER_TELEGRAM_ID`, and only in a private chat, for both messages and button presses |
| Cookies in the chat | Your message is deleted first, before anything else runs. Anything cookie-looking sent outside `/connect` is deleted with a warning |
| Cookie files | Optional exports in `secrets/` (`chmod 700/600`, git-ignored), mounted read-only into the bot. Once imported, they're stored encrypted like everything else |
| Cookies at rest | AES-256-GCM. The key is `ENCRYPTION_KEY` in `.env.local` (`chmod 600`). Each ciphertext is bound to `platform:username` |
| Cookies in transit | Sent only to fixed `x.com` / `instagram.com` addresses, with redirects disabled, and always through the VPN proxy (`REQUIRE_PROXY=true`) |
| Logs | Request logging is off. Network exceptions are stripped so they can't carry the Cookie header |
| Container | Runs as your own non-root user (set by `./dc`) with a read-only filesystem, all Linux capabilities dropped, and `no-new-privileges`. No published ports |
| Known limit | The key and the database sit on the same machine, so the encryption mainly protects copies of the database such as backups. Keep backups of `data/` and `.env.local` separate |

An independent security review found no Critical or High issues. All Low findings are fixed.
