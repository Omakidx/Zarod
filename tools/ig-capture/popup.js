const CAPTURE_MS = 60 * 1000;

const $ = (id) => document.getElementById(id);

async function render() {
  const { until = 0, items = [], seen = {} } = await chrome.storage.session.get(["until", "items", "seen"]);
  const total = Object.values(seen).reduce((a, b) => a + b, 0);
  const left = Math.max(0, Math.round((until - Date.now()) / 1000));
  const hits = items.filter((i) => i.likelyFollow).length;
  $("status").textContent = left
    ? `● Capturing (${left}s left): ${total} requests seen, ${items.length} POSTs, ${hits} look like a follow`
    : `Stopped: ${total} requests seen, ${items.length} POSTs, ${hits} look like a follow`;
  $("list").replaceChildren(
    ...items.map((i) => {
      const li = document.createElement("li");
      li.textContent = `${i.status ?? i.error} ${i.path}${i.friendlyName ? ` (${i.friendlyName})` : ""}`;
      if (i.likelyFollow) li.className = "hit";
      return li;
    }),
  );
}

$("start").onclick = async () => {
  await chrome.storage.session.set({ until: Date.now() + CAPTURE_MS, items: [], seen: {} });
  render();
};

$("clear").onclick = async () => {
  await chrome.storage.session.set({ until: 0, items: [], seen: {} });
  render();
};

$("copy").onclick = async () => {
  const { items = [], seen = {} } = await chrome.storage.session.get(["items", "seen"]);
  const ordered = [...items.filter((i) => i.likelyFollow), ...items.filter((i) => !i.likelyFollow)];
  const report = {
    note: "Zarod IG Request Capture: names and shapes only; cookie and token values removed in the browser.",
    seen,
    items: ordered,
  };
  await navigator.clipboard.writeText(JSON.stringify(report, null, 1));
  $("copy").textContent = "✅ Copied";
  setTimeout(() => ($("copy").textContent = "📋 Copy report"), 1500);
};

render();
setInterval(render, 1000);
