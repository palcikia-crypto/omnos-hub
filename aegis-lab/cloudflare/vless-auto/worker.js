import { connect } from "cloudflare:sockets";

const UUID = "f30d23f3-92c3-4147-86de-b76f36f718ed";
const DOH = "https://1.1.1.1/dns-query";

export default {
  async fetch(request) {
    const upgrade = (request.headers.get("Upgrade") || "").toLowerCase();
    if (upgrade !== "websocket") {
      return new Response(JSON.stringify({ ok: true, service: "aegis-auto", uuid: UUID }, null, 2),
        { headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
    }
    let early = "";
    try {
      const p = request.headers.get("sec-websocket-protocol") || "";
      if (p) early = atob(p.replace(/-/g, "+").replace(/_/g, "/"));
    } catch {}
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    handle(server, early).catch(() => { try { server.close(1011, "err"); } catch {} });
    return new Response(null, { status: 101, webSocket: client });
  },
};

async function handle(server, early) {
  let buf = early ? Uint8Array.from(early, c => c.charCodeAt(0) & 0xff) : new Uint8Array(0);
  let gotHeader = false;
  let udpMode = false;
  let remoteWriter = null;
  let vlessVersion = 0;

  const parseDatagram = (data) => {
    if (data.length >= 2) {
      const l = (data[0] << 8) | data[1];
      if (l + 2 <= data.length) return data.subarray(2, 2 + l);
    }
    return data;
  };

  const wrapDatagram = (data) => {
    const out = new Uint8Array(2 + data.length);
    out[0] = data.length >> 8;
    out[1] = data.length & 0xff;
    out.set(data, 2);
    return out;
  };

  const forwardDns = async (payload) => {
    const q = parseDatagram(payload);
    const r = await fetch(DOH, {
      method: "POST",
      headers: { "Content-Type": "application/dns-message" },
      body: q,
    });
    const resp = new Uint8Array(await r.arrayBuffer());
    if (server.readyState === 1) server.send(wrapDatagram(resp));
  };

  const onChunk = (chunk) => {
    if (chunk && chunk.byteLength > 0) {
      if (gotHeader) {
        if (udpMode) {
          forwardDns(new Uint8Array(chunk)).catch(() => {});
        } else if (remoteWriter) {
          remoteWriter.write(chunk).catch(() => {});
        }
        return;
      }
      const incoming = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
      const nb = new Uint8Array(buf.length + incoming.length);
      nb.set(buf);
      nb.set(incoming, buf.length);
      buf = nb;
    }
    if (gotHeader || buf.length === 0) return;
    if (buf.length < 18) return;
    vlessVersion = buf[0];
    const uuid = Array.from(buf.subarray(1, 17))
      .map(b => b.toString(16).padStart(2, "0")).join("");
    if (uuid !== UUID.replaceAll("-", "")) {
      server.close(1008, "bad uuid");
      gotHeader = true;
      return;
    }
    const olen = buf[17];
    if (buf.length < 18 + olen + 4) return;
    const cmd = buf[18 + olen];
    const port = (buf[19 + olen] << 8) | buf[20 + olen];
    const atype = buf[21 + olen];
    let p = 22 + olen;
    let hostname = "";
    if (atype === 1) {
      if (buf.length < p + 4) return;
      hostname = Array.from(buf.subarray(p, p + 4)).join(".");
      p += 4;
    } else if (atype === 2) {
      if (buf.length < p + 1) return;
      const dl = buf[p];
      p += 1;
      if (buf.length < p + dl) return;
      hostname = new TextDecoder().decode(buf.subarray(p, p + dl));
      p += dl;
    } else if (atype === 3) {
      if (buf.length < p + 16) return;
      const parts = [];
      for (let i = 0; i < 16; i += 2) parts.push(((buf[p + i] << 8) | buf[p + i + 1]).toString(16));
      hostname = parts.join(":");
      p += 16;
    } else {
      server.close(1008, "bad address type");
      gotHeader = true;
      return;
    }

    gotHeader = true;
    if (cmd === 2 || cmd === 3) {
      udpMode = true;
      server.send(new Uint8Array([vlessVersion, 0]));
      if (buf.length > p) forwardDns(buf.subarray(p)).catch(() => {});
      return;
    }
    (async () => {
      let remote;
      try {
        remote = connect({ hostname: hostname, port: port });
      } catch (e) {
        if (server.readyState === 1) server.send(new TextEncoder().encode("CONNECT-ERR: " + String(e)));
        try { server.close(1011, "connect failed"); } catch {}
        return;
      }
      const w = remote.writable.getWriter();
      remoteWriter = w;
      const rest = buf.subarray(p);
      if (rest.length) {
        try {
          await w.write(rest);
        } catch (e) {
          if (server.readyState === 1) server.send(new TextEncoder().encode("WRITE-ERR: " + String(e)));
        }
      }
      if (server.readyState === 1) server.send(new Uint8Array([vlessVersion, 0]));
      remote.readable.pipeTo(new WritableStream({
        write(chunk2) { if (server.readyState === 1) server.send(chunk2); },
        close() { try { server.close(); } catch {} },
        abort() { try { server.close(); } catch {} },
      })).catch(() => { try { server.close(); } catch {} });
    })();
  };

  if (buf.length) onChunk(new Uint8Array(0));
  server.addEventListener("message", (e) => onChunk(e.data));
  server.addEventListener("close", () => { try { remoteWriter?.releaseLock(); } catch {} });
  server.addEventListener("error", () => {});
}
