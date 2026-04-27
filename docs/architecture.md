# irc-lens architecture (work in progress)

> **Status:** stub. Phase 10 produces the full architecture document
> (runtime diagram, module layout, decision log). For now this file
> exists so the vendored frontend assets have a documented pinned
> version, per the build plan's Phase 4 requirement.

## Vendored frontend assets

`irc-lens` ships with HTMX vendored under
`src/irc_lens/static/vendor/`, not loaded from a CDN. Rationale:
the lens runs on localhost, drives Playwright in offline-friendly
agent loops, and must boot deterministically without outbound network.
The assets are wheel-shipped via `tool.hatch.build.targets.wheel`'s
package include.

| File | Pin | Source |
| --- | --- | --- |
| `htmx.min.js`  | `htmx.org@2.0.4`        | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` |
| `sse.js`       | `htmx-ext-sse@2.2.2`    | `https://unpkg.com/htmx-ext-sse@2.2.2/sse.js` |

To refresh, run:

```bash
curl -fsSL https://unpkg.com/htmx.org@<VERSION>/dist/htmx.min.js \
  -o src/irc_lens/static/vendor/htmx.min.js
curl -fsSL https://unpkg.com/htmx-ext-sse@<VERSION>/sse.js \
  -o src/irc_lens/static/vendor/sse.js
```

…and update the version pins in this table. Don't bump versions
without verifying the SSE event-listener API still matches what
`src/irc_lens/static/lens.js` expects (currently a vanilla
`EventSource` listener; HTMX's SSE wiring is added in Phase 7).
