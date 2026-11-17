// Turns a captured request into a report that keeps its shape (URL, header names, field names)
// and drops every value that could be a secret. Only values on the allowlists below are kept.

const SAFE_HEADER_VALUES = new Set([
  "accept",
  "content-type",
  "origin",
  "sec-fetch-dest",
  "sec-fetch-mode",
  "sec-fetch-site",
  "x-asbd-id",
  "x-fb-friendly-name",
  "x-ig-app-id",
  "x-instagram-ajax",
  "x-requested-with",
  "x-root-field-name",
]);

// Operation names and fixed client flags; nothing tied to your session.
const SAFE_FIELD_VALUES = new Set([
  "__a",
  "__comet_req",
  "__d",
  "container_module",
  "doc_id",
  "fb_api_caller_class",
  "fb_api_req_friendly_name",
  "server_timestamps",
]);

const SAFE_QUERY_VALUES = new Set(["__a", "__d", "doc_id"]);

const FOLLOW_HINT = /friendships|follow|graphql|query/i;

export function shape(value) {
  if (typeof value === "boolean" || value === null) return value;
  if (typeof value === "number") return "<number>";
  if (typeof value === "string") {
    return /^\d+$/.test(value) ? `<digits:${value.length}>` : `<text:${value.length}>`;
  }
  if (Array.isArray(value)) return value.slice(0, 5).map(shape);
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, shape(v)]));
  }
  return `<${typeof value}>`;
}

function field(name, value) {
  if (SAFE_FIELD_VALUES.has(name)) return value;
  if (name === "variables") {
    try {
      return { json: shape(JSON.parse(value)) };
    } catch {
      return shape(value);
    }
  }
  return shape(value);
}

function bodyFromPairs(pairs) {
  const fields = {};
  for (const [name, value] of pairs) fields[name] = field(name, value);
  return fields;
}

export function summarizeBody(requestBody) {
  if (!requestBody) return { kind: "none" };
  if (requestBody.error) return { kind: "error", error: requestBody.error };
  if (requestBody.formData) {
    const pairs = Object.entries(requestBody.formData).map(([k, v]) => [k, Array.isArray(v) ? v[0] : v]);
    return { kind: "form", fields: bodyFromPairs(pairs) };
  }
  if (requestBody.raw && requestBody.raw.length) {
    const bytes = requestBody.raw.filter((p) => p.bytes).map((p) => new Uint8Array(p.bytes));
    const text = new TextDecoder().decode(bytes.length === 1 ? bytes[0] : concat(bytes));
    try {
      return { kind: "json", fields: shape(JSON.parse(text)) };
    } catch {
      return { kind: "urlencoded", fields: bodyFromPairs(new URLSearchParams(text).entries()) };
    }
  }
  return { kind: "unknown" };
}

function concat(chunks) {
  const out = new Uint8Array(chunks.reduce((n, c) => n + c.length, 0));
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}

export function summarizeHeaders(requestHeaders = []) {
  const headers = {};
  let cookieNames = [];
  for (const { name, value } of requestHeaders) {
    const key = name.toLowerCase();
    if (key === "cookie") {
      cookieNames = (value || "").split(";").map((c) => c.split("=")[0].trim()).filter(Boolean);
      headers[key] = "<redacted>";
    } else {
      headers[key] = SAFE_HEADER_VALUES.has(key) ? value : "<redacted>";
    }
  }
  return { headers, cookieNames };
}

export function summarize({ url, method, requestBody, requestHeaders, statusCode, responseHeaders, error }) {
  const u = new URL(url);
  const query = {};
  for (const [k, v] of u.searchParams) query[k] = SAFE_QUERY_VALUES.has(k) ? v : shape(v);
  const { headers, cookieNames } = summarizeHeaders(requestHeaders);
  const body = summarizeBody(requestBody);
  const responseType = (responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type")?.value || "";
  const friendly =
    headers["x-fb-friendly-name"] || (body.fields && body.fields.fb_api_req_friendly_name) || "";
  return {
    method,
    host: u.host,
    path: u.pathname,
    query,
    status: statusCode ?? null,
    error: error || null,
    responseType: responseType.split(";")[0],
    friendlyName: friendly,
    likelyFollow: FOLLOW_HINT.test(u.pathname) && (!friendly || /follow/i.test(friendly) || /friendships/.test(u.pathname)),
    headers,
    cookieNames,
    body,
  };
}
