// Chat edge Worker: one active origin, failover by cron, an outage page when it's down.
// Secrets and hostnames are bindings: see README.md.
import outageHtml from "./outage.html";
import { checkOrigins, handleRequest } from "./handler.mjs";

export default {
  fetch: (request, env) => handleRequest(request, env, { outageHtml }),
  scheduled: (_event, env, ctx) => ctx.waitUntil(checkOrigins(env)),
};
