// Request and cron handling, separate from worker.mjs so node can test it with stubs.
import {
  DEFAULT_STATE,
  EDGE_TOKEN_HEADER,
  isGatewayFailure,
  nextState,
  originHeaders,
  originHost,
  parseState,
  sameState,
  wantsPage,
} from "./logic.mjs";

export const STATE_KEY = "failover-state";
const READY_TIMEOUT_MS = 5000;
const DEFAULT_THRESHOLD = 3;

function outage(request, outageHtml) {
  const headers = { "Cache-Control": "no-store", "Retry-After": "120" };
  if (wantsPage(request)) {
    return new Response(outageHtml, {
      status: 503,
      headers: { ...headers, "Content-Type": "text/html; charset=utf-8" },
    });
  }
  return Response.json({ error: "temporarily unavailable" }, { status: 503, headers });
}

// Read as text, so a mistyped hand-written state is told apart from a KV failure: bad
// content falls back to the default, and a KV failure still throws to the caller.
async function readState(env) {
  const raw = await env.STATE.get(STATE_KEY);
  if (raw === null) return { ...DEFAULT_STATE };
  try {
    return parseState(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_STATE };
  }
}

// Last state this isolate read, for serving through a KV failure.
let lastKnownState = { ...DEFAULT_STATE };

async function stateForRequest(env) {
  try {
    lastKnownState = await readState(env);
  } catch {
    // Keep serving: a KV failure must not take down a healthy origin.
  }
  return lastKnownState;
}

export async function handleRequest(request, env, { fetchImpl = fetch, outageHtml }) {
  const state = await stateForRequest(env);
  const forwarded = new Request(request.url, {
    method: request.method,
    headers: originHeaders(request.headers, env),
    body: request.body,
    redirect: "manual",
    duplex: "half",
  });
  let response;
  try {
    // Keeps the public Host, so the lens's Origin check still matches (d2).
    response = await fetchImpl(forwarded, { cf: { resolveOverride: originHost(state, env) } });
  } catch {
    return outage(request, outageHtml);
  }
  // Passed through as is: SSE must not be buffered.
  return isGatewayFailure(response.status) ? outage(request, outageHtml) : response;
}

async function ready(host, env, fetchImpl) {
  try {
    const response = await fetchImpl(`https://${host}/readyz`, {
      headers: { [EDGE_TOKEN_HEADER]: env.EDGE_TOKEN },
      // A redirect isn't readiness, and following one would send the token elsewhere.
      redirect: "manual",
      signal: AbortSignal.timeout(READY_TIMEOUT_MS),
    });
    return response.status === 200;
  } catch {
    return false;
  }
}

export async function checkOrigins(env, fetchImpl = fetch) {
  const state = await readState(env);
  const [primaryOk, standbyOk] = await Promise.all([
    ready(env.ORIGIN_PRIMARY, env, fetchImpl),
    ready(env.ORIGIN_STANDBY, env, fetchImpl),
  ]);
  const threshold = Number(env.FAILOVER_THRESHOLD) || DEFAULT_THRESHOLD;
  const next = nextState(state, { primaryOk, standbyOk }, threshold);
  // Only write on change: the Free plan allows 1,000 KV writes a day.
  if (!sameState(state, next)) await env.STATE.put(STATE_KEY, JSON.stringify(next));
  return next;
}
