// Watches instagram.com POST requests while capture is on and stores redacted summaries.
// Nothing is sent anywhere: the report only leaves the browser when you press "Copy report".

import { summarize } from "./redact.js";

const FILTER = { urls: ["*://*.instagram.com/*", "*://*.cdninstagram.com/*", "*://*.facebook.com/*"] };
const MAX_ITEMS = 60;
const pending = new Map();

// How many requests the extension sees at all, so an empty report can be told apart from a broken capture.
let tally = null;
async function count(method) {
  if (!(await capturing())) return;
  tally ??= (await chrome.storage.session.get("seen")).seen || {};
  tally[method] = (tally[method] || 0) + 1;
  await chrome.storage.session.set({ seen: tally });
}

chrome.storage.session.onChanged.addListener((changes) => {
  if (changes.seen && !Object.keys(changes.seen.newValue || {}).length) tally = null;
});

async function capturing() {
  const { until = 0 } = await chrome.storage.session.get("until");
  return Date.now() < until;
}

chrome.webRequest.onBeforeRequest.addListener(
  (d) => {
    count(d.method);
    if (d.method === "POST") pending.set(d.requestId, { url: d.url, method: d.method, requestBody: d.requestBody });
  },
  FILTER,
  ["requestBody"],
);

chrome.webRequest.onBeforeSendHeaders.addListener(
  (d) => {
    const p = pending.get(d.requestId);
    if (p) p.requestHeaders = d.requestHeaders;
  },
  FILTER,
  ["requestHeaders", "extraHeaders"],
);

async function finish(d, extra) {
  const p = pending.get(d.requestId);
  pending.delete(d.requestId);
  if (!p || !(await capturing())) return;
  const item = { time: new Date().toISOString(), ...summarize({ ...p, ...extra }) };
  const { items = [] } = await chrome.storage.session.get("items");
  items.push(item);
  await chrome.storage.session.set({ items: items.slice(-MAX_ITEMS) });
}

chrome.webRequest.onCompleted.addListener(
  (d) => finish(d, { statusCode: d.statusCode, responseHeaders: d.responseHeaders }),
  FILTER,
  ["responseHeaders"],
);

chrome.webRequest.onErrorOccurred.addListener((d) => finish(d, { error: d.error }), FILTER);
