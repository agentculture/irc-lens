# Chat edge Worker

It sits in front of the chat console, sends traffic to one active origin, fails over, and shows an outage page.

## How it decides

Consecutive failed `/readyz` checks by cron trigger failover. The default threshold is 3, set by `FAILOVER_THRESHOLD`. Returning to the primary is deliberate. The one automatic return is when the standby is failing and the primary is healthy.

## Bindings

| Name | Kind | Purpose |
| --- | --- | --- |
| STATE | KV namespace | Stores the current failover state |
| ORIGIN_PRIMARY | plain text | The primary tunnel hostname |
| ORIGIN_STANDBY | plain text | The standby tunnel hostname |
| EDGE_TOKEN | secret | The credential the lens checks |
| FAILOVER_THRESHOLD | optional | Consecutive failures before failing over |

## Failing back

To return to the primary, write this to the KV key `failover-state`: `{"active":"primary","failures":0}`.

## Testing

Run `node --test logic.test.mjs handler.test.mjs` from this directory.

## Headers

The Worker sets `X-Lens-Edge-Token` and `X-Lens-Edge-Client-IP`, overwriting whatever a visitor sent. The lens trusts them only when the token verifies.

## Deploying

The Worker needs the `outage.html` file bundled as a text module. Deploy it with cultureflare's scripts, dry-run first, as the owner. Don't name any real hostname anywhere in this file.
