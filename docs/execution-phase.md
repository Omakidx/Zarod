# Execution phases

Every step from here to using the bot day to day. Everything is deployed and tested **on this machine**, using one pinned Surfshark location. 🙋 marks a step that needs **you**: Claude can't do it, or shouldn't, because it involves your accounts, money, or secrets. Claude asks you at each 🙋 checkpoint before moving on.

**Ground rule for secrets:** they go only in **`.env.local`** (`chmod 600`, git-ignored), in the **`secrets/`** folder (`chmod 700`, git-ignored; for cookie exports), or straight into the bot's Telegram chat. **Never paste a token, key, or cookie into a chat with Claude.** At each 🙋 step, just tell Claude "done". Claude checks that a value is *present* in `.env.local` without ever reading it.

**All Docker commands go through `./dc`.** It's `docker compose` with `.env.local` loaded, and it runs the bot as your own user, so `./data` needs no `sudo`.

| # | Phase | Who | Status |
|---|---|---|---|
| 0 | Build and offline validation | Claude | ✅ Done |
| 1 | Fill in `.env.local` and prepare test accounts | 🙋 You | ✅ Done |
| 2 | Deploy here and check the VPN | Claude, then 🙋 you | ✅ Done (exits in Ashburn, US) |
| 3 | Live probe on throwaway accounts | 🙋 You + Claude | ✅ Done (X and Instagram) |
| 4 | Telegram end-to-end test | 🙋 You + Claude | ⏳ X single + bulk pass · Instagram single pass · commands and Instagram bulk pending |
| 5 | Soak test (2–3 days) | You | Waiting on 4 |
| 6 | Go live with your real accounts | 🙋 You | Waiting on 5 |
| 7 | Ongoing operations | You | — |

---

## Phase 0: Build and offline validation ✅

- All code is written. **50 offline tests pass**, the Docker image builds, and the compose file is valid.
- An independent security review is done and its findings are fixed.
- `.env.local` has been created from the template with `chmod 600`, and `ENCRYPTION_KEY` is already generated inside it. It was never displayed.
- **Not yet verified:** any real call to Telegram, X, Instagram, or Surfshark. Phases 2–4 cover that.

---

## Phase 1: Fill in `.env.local` and prepare test accounts 🙋

Open `.env.local` in your editor and fill in the following:

| # | What | Where to get it | Key in `.env.local` |
|---|---|---|---|
| 1.1 | 🙋 **Telegram bot token** | [@BotFather](https://t.me/BotFather) → `/newbot` | `TELEGRAM_BOT_TOKEN` |
| 1.2 | 🙋 **Your Telegram user ID** (a number, not your @username) | [@userinfobot](https://t.me/userinfobot) | `OWNER_TELEGRAM_ID` |
| 1.3 | 🙋 **Surfshark WireGuard private key** | Surfshark dashboard → VPN → Manual setup → Router → WireGuard → "I don't have a key pair" → generate. Copy the **private** key | `SURFSHARK_WG_PRIVATE_KEY` |
| 1.4 | 🙋 **One Surfshark server to pin.** Pick one in your own country if you can (command below to list them) | e.g. `us-nyc.prod.surfshark.com` | `VPN_HOSTNAME` |
| 1.5 | 🙋 **Your browser's user agent.** Use the browser you'll export cookies from. Search "what is my user agent" in it. If one platform's cookies come from a different browser (e.g. Instagram from Brave), put that browser's user agent in the platform key too | Your browser | `BROWSER_USER_AGENT` (+ optional `X_USER_AGENT`, `INSTAGRAM_USER_AGENT`) |
| 1.6 | ✅ **Encryption key.** Already generated. Keep a copy somewhere safe and offline: if it's lost, you have to reconnect every account | — | `ENCRYPTION_KEY` |
| 1.7 | 🙋 **Throwaway test accounts:** one X and one Instagram account you can afford to lose. Use them normally for a day or two first | You | — (cookies go in at phase 3) |
| 1.7b | 🙋 **Cookie exports (optional, instead of `/connect`).** Save each export as its own file in `secrets/` (`chmod 600`) and name it in `.env.local`. **Never paste the JSON into `.env.local` itself**: an env file allows only one line per key | Your cookie extension | `X_COOKIES_FILE=x.cookies.json`, `INSTAGRAM_COOKIES_FILE=instagram.cookies.json` |
| 1.8 | 🙋 **A short list of accounts to follow in tests:** 3–5 public, 1 private, and 1 handle that doesn't exist | You | — |

To list the Surfshark servers for one country (replace "germany"):

```bash
docker run --rm qmcgaw/gluetun:v3.40.0 format-servers -surfshark | grep -i "germany"
```

You can leave the rate limits at their defaults for now. They're lowered in phase 6.

**Done when:** keys 1.1–1.5 are filled in, and the test accounts exist.
**Claude will ask:** "Is `.env.local` filled in?" Answer yes/no only, and don't send values. Claude then checks that each key is non-empty, without reading the values.

---

## Phase 2: Deploy here and check the VPN

Claude runs these steps on this machine:

1. Build and start everything:
   ```bash
   ./dc up -d --build
   ```
2. Check that the VPN connected and see which IP it exits from:
   ```bash
   ./dc logs vpn | grep -i "public ip"
   ```
3. Check that the bot started:
   ```bash
   ./dc logs bot | tail -20
   ```
4. 🙋 **Your part:** send `/start` to your bot. It should answer. Optionally, have someone else message it too: it should ignore them.

**Done when:** gluetun logs a public IP in the country you chose, and the bot answers your `/start` and nobody else's.
**If it fails:** Claude reads the logs (they contain no secrets) and fixes the problem.

The containers restart on their own after a reboot, because Docker is enabled at boot. The bot only runs while this machine is on.

---

## Phase 3: Live probe on throwaway accounts 🙋

This is the first real contact with X and Instagram. Telegram isn't involved yet.

1. 🙋 In your browser, **connect Surfshark to the same server** you put in `VPN_HOSTNAME`. Use the Surfshark browser extension, or the app's server picker.
2. 🙋 In that browser, log in to the **throwaway** X account and export the cookies as JSON with your extension. Save the export as `secrets/x.cookies.json`, then:
   ```bash
   chmod 600 secrets/x.cookies.json
   ```
   The `secrets/` folder is git-ignored.
3. Check the session only. This sends no follow:
   ```bash
   ./dc run --rm -T bot python -m followbot probe x - --location main < secrets/x.cookies.json
   ```
   Expected: `logged in as @your_test_account`
4. Follow **one** account from your test list:
   ```bash
   ./dc run --rm -T bot python -m followbot probe x - --location main --follow SOME_HANDLE < secrets/x.cookies.json
   ```
   Expected: `@SOME_HANDLE: followed (request sent: True)`. Check on x.com that it really happened.
5. Repeat steps 2–4 for Instagram, with `secrets/instagram.cookies.json` and `probe instagram`.
6. Keep the files if `X_COOKIES_FILE` / `INSTAGRAM_COOKIES_FILE` point to them, because the bot imports them at startup. Otherwise delete them:
   ```bash
   shred -u secrets/x.cookies.json secrets/instagram.cookies.json
   ```

Claude can run steps 3–4 for you once you say the files are in place. Claude runs only the command and never opens the files.

**Done when:** both platforms show `logged in` and one real follow each.
**If it fails:** the probe prints one line and never prints cookies. Likely fixes:

| Output | Likely cause | What happens next |
|---|---|---|
| `SessionExpired` right away on X | X now wants extra headers (`x-client-transaction-id`) | Claude switches the X adapter to twikit (about 1 hour of work) and you rerun phase 3 |
| `SessionExpired` on Instagram | The cookies were made on a different IP, or the user agent doesn't match | Re-export through the same Surfshark server and check `BROWSER_USER_AGENT` |
| `ActionBlocked` | The platform wants a verification | Open the app, confirm it's you, wait a few hours, retry |
| `TransientError: …` | The VPN container is down or still connecting | Check `./dc ps` and `./dc logs vpn` |

---

## Phase 4: Telegram end-to-end test 🙋

Still on the **throwaway** accounts.

1. Accounts named in `X_COOKIES_FILE` / `INSTAGRAM_COOKIES_FILE` are imported when the bot starts, and it messages you "✅ Imported X @…". 🙋 For any account not set up that way, send `/connect` → X → paste the cookie JSON. Check that the bot deletes your message and replies "Connected as @…".
2. Send `/follow` with **one** handle. It should be followed within about a minute.
3. Send `/follow` with your mixed test list: public and private accounts, a handle that doesn't exist, a duplicate, and one link for the other platform. Check that the preview counts are right, then press Start.
4. Send a **bulk list of about 20 handles.** Check:
   - no more than 14 follows in any 10 minutes;
   - the progress message updates;
   - the final report lists the not-found and private accounts.
5. Try `/pause`, `/resume`, `/queue`, `/accounts`, and remove-then-reconnect.

While you do this, Claude watches `./dc logs -f bot` and the database counters (which contain no secrets) to confirm the pacing.

**Done when:** every check above passes.

---

## Phase 5: Soak test (2–3 days)

- Keep this machine on and leave the test accounts running a small bulk list each day (about 30–50).
- Watch for ⏸ (rate limit) and 🔑 (expired) alerts in Telegram.
- Write down how long the cookies last before a 🔑 alert. That tells you how often you'll need to `/connect` again.

**Done when:** 2–3 days pass with no ⛔ blocks. If a ⏸ cooldown shows up, lower the caps.

---

## Phase 6: Go live with your real accounts 🙋

1. 🙋 **Lower the caps for a warm-up week.** Edit `.env.local`:
   ```
   DAILY_LIMIT_X=50
   DAILY_LIMIT_INSTAGRAM=30
   ```
   Then apply them:
   ```bash
   ./dc up -d
   ```
2. 🙋 Export your **real** account cookies through the pinned Surfshark server, then `/connect`. Remove the test accounts from `/accounts` if you like.
3. After a clean week, raise the caps step by step (for example X 100 → 200, Instagram 60 → 100). Don't go above 200/day on Instagram or 400/day on X.

**Your call:** whether, and how much, risk to take with your main accounts.

---

## Phase 7: Ongoing operations

| Task | How often | Command / action |
|---|---|---|
| Refresh expired cookies | When a 🔑 alert arrives | Re-export (through the same Surfshark server), then either `/connect`, or overwrite the file in `secrets/` and run `./dc restart bot` |
| Back up | Weekly | Copy `data/` somewhere. Store `ENCRYPTION_KEY` **separately** |
| Read logs | When something looks off | `./dc logs --tail 200 bot` |
| Stop / start | Any time | `./dc down` / `./dc up -d` |
| Update | Monthly | Bump the versions in `requirements.txt` and the gluetun tag, run the tests, then `./dc up -d --build` |
| Keep it running | Always | The bot runs only while this machine is on. Docker restarts both containers on boot, and queued follows continue from where they stopped |

---

## Optional later work (not started)

- A second Surfshark location: add a second gluetun service and a `PROXIES` entry.
- Pin the base images by digest and use `pip --require-hashes`. This hardens the supply chain.
- Unfollow, or "follow these at a set time".
- More platforms (Threads, TikTok) as new adapters in `followbot/platforms/`.
- Switch X to twikit, if phase 3 shows it's needed.
