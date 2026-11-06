// Pure decisions for the chat edge Worker: no network, so node can test them directly.

export const PRIMARY = "primary";
export const STANDBY = "standby";
export const DEFAULT_STATE = Object.freeze({ active: PRIMARY, failures: 0 });

// Client headers this Worker owns: a visitor's copy must never reach the lens.
export const CLIENT_IP_HEADER = "X-Lens-Edge-Client-IP";
export const EDGE_TOKEN_HEADER = "X-Lens-Edge-Token";

// No 503: the lens answers 503 itself in standby mode and on some forms, and those must pass.
const GATEWAY_FAILURES = new Set([502, 504, 520, 521, 522, 523, 524, 525, 526, 530]);

/**
 * Next failover state after one readiness check of both origins.
 *
 * spark to spark2 takes `threshold` consecutive failures, so a restart moves no one.
 * Returning to spark is a deliberate step, made by writing the state by hand. The one
 * exception is a failing standby with a healthy primary: serving beats an outage page.
 */
export function nextState(state, { primaryOk, standbyOk }, threshold) {
  const activeOk = state.active === PRIMARY ? primaryOk : standbyOk;
  if (activeOk) return { active: state.active, failures: 0 };

  // Capped so a long outage stops changing the state, and so stops writing to KV.
  const failures = Math.min(state.failures + 1, threshold);
  const otherOk = state.active === PRIMARY ? standbyOk : primaryOk;
  if (failures >= threshold && otherOk) {
    return { active: state.active === PRIMARY ? STANDBY : PRIMARY, failures: 0 };
  }
  return { active: state.active, failures };
}

export function sameState(a, b) {
  return a.active === b.active && a.failures === b.failures;
}

export function parseState(raw) {
  if (raw && (raw.active === PRIMARY || raw.active === STANDBY) && Number.isInteger(raw.failures)) {
    return { active: raw.active, failures: raw.failures };
  }
  return { ...DEFAULT_STATE };
}

/** The tunnel hostname that the active origin is reached through. */
export function originHost(state, env) {
  return state.active === PRIMARY ? env.ORIGIN_PRIMARY : env.ORIGIN_STANDBY;
}

/**
 * Headers for the origin. Overwrites the Worker's own headers, whatever the visitor sent,
 * because the lens trusts them once the token verifies.
 */
export function originHeaders(incoming, env) {
  const headers = new Headers(incoming);
  headers.delete(EDGE_TOKEN_HEADER);
  headers.delete(CLIENT_IP_HEADER);
  headers.set(EDGE_TOKEN_HEADER, env.EDGE_TOKEN);
  const clientIp = incoming.get("CF-Connecting-IP");
  if (clientIp) headers.set(CLIENT_IP_HEADER, clientIp);
  return headers;
}

/** True when a response means the origin couldn't be reached, not an app answer. */
export function isGatewayFailure(status) {
  return GATEWAY_FAILURES.has(status);
}

/** Only a page load gets the outage page: scripts expect their usual responses. */
export function wantsPage(request) {
  return request.method === "GET" && (request.headers.get("Accept") || "").includes("text/html");
}
