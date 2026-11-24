# My use flow

How you use the bot day to day once it's running. Every step happens inside Telegram, from the **menu buttons** under the message box (send `/start` once if you don't see them):

```
[        ➕ Follow accounts        ]
[ 👥 My accounts ] [ 🗂 Queue      ]
[         🔗 Add account          ]
[ ⏸ Pause all   ] [ ▶️ Resume all ]
[ 🗑 Clear queue ] [ ❓ Help       ]
```

The menu uses plain emoji on purpose. With coloured buttons and custom-emoji icons in the menu, Telegram Desktop stopped sending messages in the chat (they hung with a clock icon) while the phone app was fine. The buttons inside messages (Start, Cancel, and so on) still use the *Telegram iOS Icons* pack; Telegram shows those only if the bot is allowed custom emoji, otherwise the bot drops them and they work the same. The old `/commands` still work, but you don't need them.

## 1. Connect an account (once per account, and again when cookies expire)

There are two ways to connect:

- **From a file (no Telegram paste).** Save the export as `secrets/x.cookies.json` (or `secrets/instagram.cookies.json`) and `chmod 600` it. Set `X_COOKIES_FILE=x.cookies.json` (or `INSTAGRAM_COOKIES_FILE=…`) in `.env.local`, then run `./dc restart bot`. The bot checks the session through the VPN and messages you **✅ Imported X @you**. It re-imports a file only when the file changes, so overwrite the file whenever you re-export.
- **In Telegram, with 🔗 Add account** (the easy way for a second or third account):

1. On your computer, **turn Surfshark on and pick the same server** the bot uses (the one in `VPN_HOSTNAME`).
2. Log in to x.com or instagram.com in your browser.
3. Click your cookie extension and **export as JSON**.
4. In the bot, tap **🔗 Add account**, choose **Instagram** or **X**. The bot gives every new account **its own Ashburn IP** automatically (locations `main`, `ash2` … `ash5`), and an account you reconnect keeps the IP it has. **Cancel** is always there if you change your mind.
5. Paste the JSON. If it's long, save it as a `.json` file and send the file instead, because Telegram splits long messages. Pasting is slightly better: a deleted upload stays on Telegram's servers.
6. The bot deletes your message, checks the session, and replies **✅ Connected as @you via 🌍 main**.

**Adding a second account on the same platform:** don't press *Log out* on the first account in your browser, because that ends its session in the bot too. Use the site's **Switch account → Add account**, or a private window, then export.

Cookies it needs:

| Platform | Cookies |
|---|---|
| X | `auth_token`, `ct0` |
| Instagram | `sessionid`, `csrftoken`, `ds_user_id` |

The bot keeps only the cookies for that site and ignores everything else in the export.

## 2. Follow

### One account
```
➕ Follow accounts
→ choose the platform: 📷 Instagram · 2 accounts / 💬 X · 1 account
→ send @someone (or a profile link)
→ preview → ✅ Start
```
**Every connected account on that platform follows the list**, all at the same time. Each account keeps its own limits (13 per 10 minutes, random gaps, daily cap) and its own IP, and each one goes through the list in its own random order, starting at a random moment in the first 10 minutes, so the accounts never move through the same profiles in lockstep.
Typing `/follow @a @b https://x.com/c` still works as a shortcut.
It goes out within about a minute, unless that account has used up its budget.

### A list
Tap **➕ Follow accounts**, choose the platform, then send any mix of these, separated by spaces, commas, or new lines:
```
@alice
bob, https://x.com/carol
https://instagram.com/dave   ← skipped if you're following on X
```
or send a `.txt` / `.csv` file with up to 500 handles.

The preview shows:
- how many will be followed, plus the first 15 names;
- duplicates removed, invalid entries, and links for the other platform;
- how many are already queued ahead of this list;
- **⏱ the ETA.** 13 follows take about 10 minutes, so 100 take about 80 minutes;
- ⚠️ a warning if the list goes past today's cap. The rest waits and runs once the cap frees up.

Press **✅ Start**. One message then updates live:

```
🔄 Job #4 · Instagram · 2 accounts
@first: 12/20 · ✅ followed 10 · ❓ not found 2
@second: 11/20 · ✅ followed 9 · 🔒 requested 1 · ❓ not found 1
⏳ 17 left
```

When every account is done, you get one message listing, per account, the accounts that were not found, unavailable, or failed.

## 3. Check on things

| Button | Shows |
|---|---|
| **🗂 Queue** | Per account: what's queued, today's count against the cap, and when the next follow goes out |
| **👥 My accounts** | Choose **Instagram** or **X**, then each of that platform's accounts with its status and VPN location, and **Pause/Resume** and **Remove** buttons |
| **✋ Pause all** · **▶️ Resume all** | Pause or resume every account at once |
| **🗑 Clear queue** | Cancel everything that hasn't been sent yet |
| **❌ Cancel** (inside a step) | Leave the step you're in. Tapping any menu button also ends it |

## 4. When the bot alerts you

| Alert | Meaning | What you do |
|---|---|---|
| ⏸ *rate limit … resuming automatically in 6 h* | The platform pushed back, so the bot backed off | **Nothing.** It resumes on its own. If this happens often, lower the daily caps in `.env.local` |
| 🔑 *session expired … tap Add account* | Your cookies stopped working | Log in again through the same VPN server, re-export, and **🔗 Add account**. The queue continues from where it stopped |
| 🗑 *locked / suspended / wants you to confirm it's you* | The platform locked, suspended or challenged the account. The bot removes it from your accounts and drops its queued follows, so jobs finish without it | Fix it in the app or website, then **➕ Add account** with fresh cookies to use it again |
| ⛔ *VPN location … no longer configured* | The `PROXIES` setting changed | Fix `.env.local`, run `./dc up -d`, then **▶️ Resume all** |

## 5. Habits that keep your accounts safe

- **Log in and export through Surfshark on Ashburn.** Each account then runs from its own fixed Ashburn IP in the bot; never move an account to another location by hand.
- Don't follow by hand from your phone in large bursts while the bot is running. Both count toward the platform's limits.
- Start a new account at low caps (see [execution-phase.md](execution-phase.md), phase 6) and raise them slowly.
- If the bot ever gets added to a group, it ignores everything there. Use it only in your private chat with it.
- Never share the bot's chat, `.env.local`, or the `data/` folder.
- The bot only runs while this machine is on. Queued follows wait and pick up again after a reboot.

## 6. Remove an account

Tap **👥 My accounts** → **🗑 Remove** → **Yes, remove**. Its encrypted cookies and anything still queued are deleted. To also end the session on the platform itself, log out of that browser session (on X: Settings → Security and account access → Apps and sessions → Sessions; on Instagram: Accounts Center → Password and security → Where you're logged in).
