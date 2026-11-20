const UPSTREAM = "https://generativelanguage.googleapis.com";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, x-goog-api-key, x-goog-api-client",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health") {
      return json({ ok: true, service: "gemini-proxy", upstream: UPSTREAM, usage: "GET /v1beta/models  |  POST /v1beta/models/{model}:generateContent  (API key via x-goog-api-key header or ?key=)" });
    }
    const target = UPSTREAM + url.pathname + url.search;
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("origin");
    headers.delete("referer");
    headers.delete("cf-connecting-ip");
    headers.delete("cf-ipcountry");
    let body = ["GET", "HEAD"].includes(request.method) ? undefined : request.body;
    let upstream;
    try {
      upstream = await fetch(target, { method: request.method, headers, body, redirect: "follow" });
    } catch (e) {
      return json({ error: { message: "upstream fetch failed: " + String(e) } }, 502);
    }
    const respHeaders = new Headers(upstream.headers);
    for (const [k, v] of Object.entries(CORS)) respHeaders.set(k, v);
    respHeaders.delete("set-cookie");
    return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}
