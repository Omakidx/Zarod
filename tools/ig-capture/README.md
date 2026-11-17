# Zarod IG Request Capture

A small Brave/Chrome extension that records **what kind of request** instagram.com sends when you click Follow, so the bot can send the same one. It keeps URLs, header names, form field names and operation names (`doc_id`, `x-fb-friendly-name`). It **removes every cookie and token value inside the browser**, before anything is shown or copied.

## Install (once)

1. In Brave, open `brave://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick this folder: `Zarod/tools/ig-capture`.
4. Pin it: puzzle-piece icon in the toolbar → pin **Zarod IG Request Capture**.

## Use

1. Open instagram.com, logged in, on a profile you don't follow yet.
2. Click the extension icon → **▶ Start capture** (it records for up to 1 minute; you can copy any time).
3. Click **Follow** on the profile.
4. Click the extension icon again → **📋 Copy report**, and paste it to Claude.

Rows in orange are the ones that look like the follow request.

## What it can see

- Only Instagram and Facebook domains (Instagram uses both), only POST requests, and only while capture is on.
- Captured items stay in the browser's memory (`storage.session`), are cleared when Brave closes, and are never sent anywhere. Copying is the only way out.
- When you're done, remove it from `brave://extensions`.

## Test

```bash
node --test tools/ig-capture/redact.test.js
```
