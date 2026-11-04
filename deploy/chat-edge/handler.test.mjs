import assert from "node:assert/strict";
import { test } from "node:test";
import { CLIENT_IP_HEADER, EDGE_TOKEN_HEADER, STANDBY } from "./logic.mjs";
import { STATE_KEY, checkOrigins, handleRequest } from "./handler.mjs";

const PAGE = "<title>down</title>";

function makeEnv(stored = null) {
  const kv = new Map(stored ? [[STATE_KEY, JSON.stringify(stored)]] : []);
  let writes = 0;
  return {
    ORIGIN_PRIMARY: "a.example.org",
    ORIGIN_STANDBY: "b.example.org",
    EDGE_TOKEN: "real-token",
    STATE: {
      get: async (key) => (kv.has(key) ? JSON.parse(kv.get(key)) : null),
      put: async (key, value) => {
        writes += 1;
        kv.set(key, value);
      },
    },
    get writes() {
      return writes;
    },
  };
}

function page(url = "https://chat.example.org/") {
  return new Request(url, { headers: { Accept: "text/html", "CF-Connecting-IP": "198.51.100.7" } });
}

test("forwards to the active origin, keeping the public host and the client IP", async () => {
  const seen = [];
  const fetchImpl = async (request, init) => {
    seen.push({ url: request.url, init, ip: request.headers.get(CLIENT_IP_HEADER), token: request.headers.get(EDGE_TOKEN_HEADER) });
    return new Response("ok");
  };
  const response = await handleRequest(page(), makeEnv(), { fetchImpl, outageHtml: PAGE });
  assert.equal(await response.text(), "ok");
  assert.equal(seen[0].url, "https://chat.example.org/");
  assert.equal(seen[0].init.cf.resolveOverride, "a.example.org");
  assert.equal(seen[0].ip, "198.51.100.7");
  assert.equal(seen[0].token, "real-token");
});

test("after failover, requests go to the standby", async () => {
  let override;
  const fetchImpl = async (_request, init) => {
    override = init.cf.resolveOverride;
    return new Response("ok");
  };
  await handleRequest(page(), makeEnv({ active: STANDBY, failures: 0 }), { fetchImpl, outageHtml: PAGE });
  assert.equal(override, "b.example.org");
});

test("a POST body reaches the origin", async () => {
  let body;
  const fetchImpl = async (request) => {
    body = await request.text();
    return new Response(null, { status: 204 });
  };
  const post = new Request("https://chat.example.org/input", { method: "POST", body: "text=hi" });
  const response = await handleRequest(post, makeEnv(), { fetchImpl, outageHtml: PAGE });
  assert.equal(response.status, 204);
  assert.equal(body, "text=hi");
});

test("a streaming response passes through unbuffered", async () => {
  const stream = new ReadableStream({ start() {} });
  const upstream = new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
  const fetchImpl = async () => upstream;
  const response = await handleRequest(page(), makeEnv(), { fetchImpl, outageHtml: PAGE });
  assert.equal(response, upstream);
});

test("an unreachable origin shows the outage page to a page load", async () => {
  const fetchImpl = async () => {
    throw new Error("connection refused");
  };
  const response = await handleRequest(page(), makeEnv(), { fetchImpl, outageHtml: PAGE });
  assert.equal(response.status, 503);
  assert.equal(await response.text(), PAGE);
});

test("a tunnel error shows the outage page, and an app error doesn't", async () => {
  for (const [status, outage] of [[530, true], [502, true], [500, false], [403, false]]) {
    const fetchImpl = async () => new Response("from origin", { status });
    const response = await handleRequest(page(), makeEnv(), { fetchImpl, outageHtml: PAGE });
    assert.equal((await response.text()) === PAGE, outage, `status ${status}`);
  }
});

test("a script gets JSON rather than the outage page", async () => {
  const fetchImpl = async () => new Response("", { status: 530 });
  const post = new Request("https://chat.example.org/input", { method: "POST", body: "x" });
  const response = await handleRequest(post, makeEnv(), { fetchImpl, outageHtml: PAGE });
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "temporarily unavailable" });
});

function probe(primary, standby) {
  return async (url) => new Response("", { status: (url.includes("a.example.org") ? primary : standby) ? 200 : 503 });
}

test("cron fails over after three failed checks, not before", async () => {
  const env = makeEnv();
  assert.equal((await checkOrigins(env, probe(false, true))).active, "primary");
  assert.equal((await checkOrigins(env, probe(false, true))).active, "primary");
  assert.equal((await checkOrigins(env, probe(false, true))).active, "standby");
});

test("cron writes nothing while healthy, staying inside the KV write limit", async () => {
  const env = makeEnv();
  for (let i = 0; i < 5; i++) await checkOrigins(env, probe(true, true));
  assert.equal(env.writes, 0);
});

test("a probe that throws counts as a failed check", async () => {
  const env = makeEnv();
  const fetchImpl = async () => {
    throw new Error("timeout");
  };
  const state = await checkOrigins(env, fetchImpl);
  assert.equal(state.failures, 1);
});
