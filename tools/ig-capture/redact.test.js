// Run with: node --test tools/ig-capture/redact.test.js
import assert from "node:assert/strict";
import test from "node:test";
import { summarize } from "./redact.js";

const SECRETS = ["SESSIONSECRET123", "CSRFSECRET456", "DTSGSECRET789", "LSDSECRET000", "CLAIMSECRET111", "26581"];

test("report keeps shape and drops every secret value", () => {
  const enc = new TextEncoder();
  const item = summarize({
    url: "https://www.instagram.com/graphql/query?doc_id=123&token=CLAIMSECRET111",
    method: "POST",
    requestHeaders: [
      { name: "Cookie", value: "sessionid=SESSIONSECRET123; csrftoken=CSRFSECRET456" },
      { name: "X-CSRFToken", value: "CSRFSECRET456" },
      { name: "X-FB-Friendly-Name", value: "usePolarisFollowMutation" },
      { name: "X-IG-WWW-Claim", value: "CLAIMSECRET111" },
      { name: "X-FB-LSD", value: "LSDSECRET000" },
      { name: "X-IG-App-ID", value: "936619743392459" },
    ],
    requestBody: {
      formData: {
        fb_dtsg: ["DTSGSECRET789"],
        lsd: ["LSDSECRET000"],
        jazoest: ["26581"],
        doc_id: ["7275591572570580"],
        fb_api_req_friendly_name: ["usePolarisFollowMutation"],
        variables: [JSON.stringify({ target_user_id: "12345678", container_module: "profile", nav_chain: "x" })],
      },
    },
    statusCode: 200,
    responseHeaders: [{ name: "Content-Type", value: "application/json; charset=utf-8" }],
  });
  const text = JSON.stringify(item);
  for (const s of SECRETS) assert.ok(!text.includes(s), `leaked ${s}`);
  assert.equal(item.path, "/graphql/query");
  assert.equal(item.query.doc_id, "123");
  assert.equal(item.friendlyName, "usePolarisFollowMutation");
  assert.equal(item.likelyFollow, true);
  assert.deepEqual(item.cookieNames, ["sessionid", "csrftoken"]);
  assert.equal(item.headers["x-ig-app-id"], "936619743392459");
  assert.equal(item.headers["x-csrftoken"], "<redacted>");
  assert.equal(item.body.fields.doc_id, "7275591572570580");
  assert.deepEqual(item.body.fields.variables.json, {
    target_user_id: "<digits:8>", container_module: "<text:7>", nav_chain: "<text:1>",
  });
  assert.equal(item.responseType, "application/json");

  const raw = summarize({
    url: "https://www.instagram.com/api/v1/friendships/create/1/",
    method: "POST",
    requestBody: { raw: [{ bytes: enc.encode("user_id=1&jazoest=26581&csrf=CSRFSECRET456").buffer }] },
  });
  assert.ok(!JSON.stringify(raw).includes("CSRFSECRET456") && !JSON.stringify(raw).includes("26581"));
  assert.equal(raw.body.kind, "urlencoded");
  assert.equal(raw.likelyFollow, true);
});
