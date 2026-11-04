import assert from "node:assert/strict";
import { test } from "node:test";
import {
  CLIENT_IP_HEADER,
  DEFAULT_STATE,
  EDGE_TOKEN_HEADER,
  PRIMARY,
  STANDBY,
  isGatewayFailure,
  nextState,
  originHeaders,
  originHost,
  parseState,
  wantsPage,
} from "./logic.mjs";

const UP = { primaryOk: true, standbyOk: true };
const PRIMARY_DOWN = { primaryOk: false, standbyOk: true };
const BOTH_DOWN = { primaryOk: false, standbyOk: false };

test("a restart shorter than the threshold moves no one", () => {
  let state = DEFAULT_STATE;
  state = nextState(state, PRIMARY_DOWN, 3);
  state = nextState(state, PRIMARY_DOWN, 3);
  assert.equal(state.active, PRIMARY);
  state = nextState(state, UP, 3);
  assert.deepEqual(state, { active: PRIMARY, failures: 0 });
});

test("repeated failures fail over, and spark's return doesn't switch back", () => {
  let state = DEFAULT_STATE;
  for (let i = 0; i < 3; i++) state = nextState(state, PRIMARY_DOWN, 3);
  assert.deepEqual(state, { active: STANDBY, failures: 0 });
  state = nextState(state, UP, 3);
  assert.equal(state.active, STANDBY);
});

test("a dead standby is never failed over to", () => {
  let state = DEFAULT_STATE;
  for (let i = 0; i < 5; i++) state = nextState(state, BOTH_DOWN, 3);
  assert.equal(state.active, PRIMARY);
  assert.equal(state.failures, 5);
});

test("a failing standby returns to a healthy primary", () => {
  let state = { active: STANDBY, failures: 0 };
  for (let i = 0; i < 3; i++) state = nextState(state, { primaryOk: true, standbyOk: false }, 3);
  assert.equal(state.active, PRIMARY);
});

test("bad stored state falls back to the primary", () => {
  for (const raw of [null, {}, { active: "other", failures: 0 }, { active: PRIMARY, failures: "x" }]) {
    assert.deepEqual(parseState(raw), { ...DEFAULT_STATE });
  }
});

test("one session stays on one origin while it's healthy (h7)", () => {
  const env = { ORIGIN_PRIMARY: "a.example.org", ORIGIN_STANDBY: "b.example.org" };
  const hosts = new Set();
  let state = DEFAULT_STATE;
  for (let i = 0; i < 100; i++) {
    state = nextState(state, UP, 3);
    hosts.add(originHost(state, env));
  }
  assert.deepEqual([...hosts], ["a.example.org"]);
});

test("a visitor can't forge the Worker's headers", () => {
  const incoming = new Headers({
    [EDGE_TOKEN_HEADER]: "forged",
    [CLIENT_IP_HEADER]: "203.0.113.66",
    "CF-Connecting-IP": "198.51.100.7",
  });
  const out = originHeaders(incoming, { EDGE_TOKEN: "real" });
  assert.equal(out.get(EDGE_TOKEN_HEADER), "real");
  assert.equal(out.get(CLIENT_IP_HEADER), "198.51.100.7");
});

test("a forged client IP is dropped when there's no real one", () => {
  const out = originHeaders(new Headers({ [CLIENT_IP_HEADER]: "203.0.113.66" }), { EDGE_TOKEN: "t" });
  assert.equal(out.get(CLIENT_IP_HEADER), null);
});

test("gateway failures are told apart from app answers", () => {
  for (const status of [502, 503, 504, 530]) assert.equal(isGatewayFailure(status), true);
  for (const status of [200, 204, 302, 401, 403, 404, 500]) assert.equal(isGatewayFailure(status), false);
});

test("only page loads get the outage page", () => {
  const page = new Request("https://example.org/", { headers: { Accept: "text/html" } });
  const post = new Request("https://example.org/input", { method: "POST" });
  assert.equal(wantsPage(page), true);
  assert.equal(wantsPage(post), false);
});
