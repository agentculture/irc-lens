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

async function readState(env) {
  const stored = await env.STATE.get(STATE_KEY, "json");
  return parseState(stored ?? DEFAULT_STATE);
}

export async function handleRequest(request, env, { fetchImpl = fetch, outageHtml }) {
  const state = await readState(env);
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
      signal: AbortSignal.timeout(READY_TIMEOUT_MS),
    });
    return response.ok;
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
