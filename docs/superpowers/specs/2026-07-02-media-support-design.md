# Media (image + audio) support — design

Status: draft, scoped 2026-07-02 with operator sign-off on the scope decisions below.
Issue: (to be filed alongside PR 1).

## Goal

Let humans and agents share images and audio through the lens, in both
directions:

- **See** — media URLs appearing in chat render inline in the console:
  `<img>` embeds for images, `<audio controls>` for audio. Plain URLs
  become clickable links.
- **Input** — the console can send media: file picker, drag-drop, paste
  from clipboard, and microphone recording. The lens stores the blob
  locally, serves it over HTTP, and posts its URL into the channel as an
  ordinary message.

**Guiding principle (operator-ratified): links over the mesh, not
binaries.** The IRC wire only ever carries a short `http(s)` URL; blobs
move over HTTP directly between consumer and host. This is easier on the
mesh and network, more reliable, and fits AgentIRC's 8192-byte inbound
line cap without any new protocol capability.

## Why URL-reference is the only viable transport

Findings from the 2026-07-02 scoping sweep of `agentirc`, `cultureagent`,
and this repo:

- AgentIRC rejects (not truncates) inbound lines over
  `MAX_INBOUND_LINE = 8192` bytes; even a tiny image as a base64 data URI
  blows past that on one line. Neither the lens transport nor the
  cultureagent harness chunks long lines.
- The IRCv3 caps that could carry inline bytes (`batch`,
  `draft/multiline`) are explicitly deferred by agentirc's 2026-07-01
  accessibility spec.
- No media/attachment/file-transfer concept exists anywhere in the mesh
  today: no verb, no tag, no blob store, no cross-machine HTTP path.

A short URL in a PRIVMSG needs zero protocol work and leaves the
protocol-side improvements (media tag hint, cross-machine reachability)
as clean, optional follow-ups.

## Ratified scope decisions (2026-07-02)

1. **v1 ships render + upload together** — one feature arc covering both
   "see" and "input".
2. **Remote embeds are click-to-load.** Lens-hosted (`/media/`) URLs
   auto-embed; media on other hosts renders as a placeholder card the
   user clicks to load. Auto-embedding remote content would leak each
   viewer's IP/headers to arbitrary hosts the moment a message renders —
   unacceptable in cloudflare-access mode. `media.trusted_hosts` widens
   auto-embed per deployment.
3. **Mic recording is in v1** (MediaRecorder → opus/webm → the same
   upload path).
4. **`GET /media/{token}` uses capability URLs** — unguessable 128-bit
   tokens, auth-exempt like `/static`. Possession of the URL means you
   saw it in the channel, matching IRC's trust model, and same-machine
   agents can fetch media *today* without credentials. Upload stays
   authed. In cloudflare-access mode, Cloudflare Access still gates the
   tunnel in front of everything.
5. **Links over the mesh, not binaries** (see guiding principle) — rules
   out any inline-bytes proposal now and in follow-ups.

## Non-goals

- Inline media bytes on the IRC wire (data URIs, chunked base64, new
  `batch`/`multiline` caps) — rejected per the guiding principle.
- URL unfurling of arbitrary web pages (og:image cards). Media is
  detected by file-extension allowlist only in v1.
- Video. The embed pipeline generalizes later; keep the v1 allowlists
  tight.
- SVG upload. SVG is a scriptable document format; served from our own
  origin on an auth-exempt route it is an XSS vector when navigated to
  directly. Excluded from the upload allowlist deliberately.
- Agent-side consumption (backends receiving images as model input) —
  that is the cultureagent follow-up below, not lens work.
- Cross-machine media-hosting guarantees. v1 advertises a configurable
  base URL; the mesh-level reachability story is the agentirc follow-up.

## Configuration surface

New optional `media` section (absent section = defaults, feature on):

```yaml
media:
  enabled: true
  # Blob store; default: $XDG_DATA_HOME/irc-lens/media
  dir: ~/.local/share/irc-lens/media
  # Per-upload cap (bytes). Default 10 MiB.
  max_file_bytes: 10485760
  # Total store cap; oldest files (by mtime) evicted past this. Default 256 MiB.
  max_store_bytes: 268435456
  # Base URL advertised in posted media links. Default derives from
  # web.bind/web.port; set to a LAN IP or the CF hostname so peers
  # beyond loopback can fetch.
  public_base_url: ""
  # click | auto | off — how media URLs from non-lens hosts render.
  remote_embeds: click
  # Hosts that auto-embed even when remote_embeds is click.
  trusted_hosts: []
```

Follows the existing loader pattern: fields on `LensConfig`, a
`_validate_media_section` beside `_validate_web_section`, keys added to
the `config init` starter template.

## Rendering path ("see")

New module `src/irc_lens/web/media.py`:

- `classify_url(url)` → `image` | `audio` | `link`, by extension
  allowlist. Images: `png jpg jpeg gif webp`. Audio:
  `mp3 ogg wav webm m4a flac`. Schemes: `http`/`https` only — `data:`,
  `javascript:`, etc. are never linkified.
- `render_message_html(text) -> Markup` — **escape-first, then
  linkify**. All safety continues to ride on escaping; the only markup
  injected is built by us from escaped components. Anchors get
  `target="_blank" rel="noopener noreferrer"`.

`_chat_line.html.j2` renders the processed text plus, when the message
contains media URLs, a `.lens-media` block under the text line:

- **Lens-hosted** (URL under `media.public_base_url` or relative
  `/media/`): direct `<img loading="lazy">` or
  `<audio controls preload="metadata">`.
- **Remote** (and `remote_embeds: click`): a placeholder card
  (`<button class="lens-media-load" data-src data-kind>`) that a new
  static module `media.js` swaps for the real element on click.
  `remote_embeds: auto` embeds directly; `off` leaves the plain link.

No SSE contract change: media rides inside the existing `chat`/`log`
fragments. CSS caps embed size (`max-width`/`max-height`) so the chat
log stays scannable. `lens.js` stays within its 120-line budget —
media behavior lives in `media.js` (the `mesh.js` precedent).

## Security headers (new; ships in PR 1)

The lens currently sets no CSP or security headers; media is exactly the
surface they protect, so they land with this feature:

- `Content-Security-Policy` on HTML responses:
  `default-src 'self'; script-src 'self'; img-src 'self' https: http:;
  media-src 'self' https: http:; object-src 'none'; base-uri 'none';
  frame-ancestors 'none'`. The load-bearing directives are `script-src`
  / `object-src`; `img-src`/`media-src` stay broad because mesh peers
  advertise plain-HTTP LAN URLs and click-to-load is the actual gate.
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`
  on all responses (the latter keeps lens URLs out of remote hosts'
  logs when a user click-loads).
- Any inline `<script>` in `index.html.j2` moves to a static file so
  `script-src 'self'` holds.

## Upload path ("input")

- **`POST /upload`** — `multipart/form-data`, field `file`. Authed and
  origin-checked exactly like `/input`. Streams to disk with a running
  byte cap; over `max_file_bytes` → 413 `{error, hint}`; type not on the
  allowlist (magic-byte sniff + extension agreement) → 400
  `{error, hint}`. Success → `201 {"url": "...", "kind": "image|audio"}`.
- `client_max_size` rises to the media cap app-wide; `POST /input`
  keeps enforcing its own 4 KiB limit in-handler (including for chunked
  bodies). The existing pinned test asserting framework-level chunked
  rejection is updated deliberately to assert the handler-level cap.
- **Store**: `dir/<principal>/<token>.<ext>`, token =
  `secrets.token_urlsafe(16)` (128-bit). Principal subdirs are for
  bookkeeping/eviction; the token is the capability. Strict
  `[A-Za-z0-9_-]+\.[a-z0-9]+` route match — no traversal surface.
- **`GET /media/{token}.{ext}`** — auth-exempt (added to the middleware
  exemptions beside `/static`). Served via `web.FileResponse` (free
  Range/ETag support — audio seeking works), `Content-Type` mapped from
  the extension allowlist only, `nosniff`,
  `Cache-Control: private, max-age=31536000, immutable`,
  `Content-Disposition: inline`.
- **Console UI** (`media.js`): attach button, drag-drop onto the chat
  area, paste-from-clipboard. On 201 the client submits the returned URL
  through the existing `/input` pipeline — one send path, so the wire
  message is an ordinary PRIVMSG carrying the URL.

## Mic recording

- Record button in the form bar. `getUserMedia` + `MediaRecorder` →
  `audio/webm;codecs=opus` (fallback `audio/mp4` where webm is
  unsupported). States: idle → recording (elapsed timer, stop) →
  uploading → sent via the same `/upload` → auto-send flow.
- Client-side max duration (5 min) so a forgotten recording can't
  produce an over-cap blob.
- Requires a secure context; both `localhost` and the CF-tunneled
  HTTPS hostname qualify.

## Wire format

The message text is simply the URL (with optional surrounding text). No
new IRC verbs, caps, or tags in v1; well under the 8192-byte inbound
cap. An optional `@agentirc.io/media` tag hint is proposed upstream
(below) so clients don't have to extension-sniff forever.

## Follow-up requests to sibling repos (drafted; file on request)

1. **cultureagent — backends consume media URLs as model input.**
   Harness detects media URLs in inbound messages, fetches with
   size/type caps, and passes image content blocks to the model. Four
   backends (`claude`/`codex`/`copilot`/`acp`) in one PR per the
   CI-enforced parity rule; graceful degradation (describe-as-link)
   where a backend lacks image support. Audio stays link-only until
   backends can hear.
2. **agentirc — media conventions spec** under
   `docs/superpowers/specs/`: (a) an optional `@agentirc.io/media`
   message-tag hint (message-tags is already a negotiated cap and tags
   don't count against the 512-byte relay budget); (b) mesh
   reachability guidance for media URLs (per-machine advertised base
   URLs; loopback-only pitfalls). Explicitly **not** an inline binary
   transport, per the links-not-binaries principle.

## Testing

- **Unit**: URL classification; escape-then-linkify (adversarial:
  `javascript:` URLs, quotes/HTML inside and around URLs); config
  validation for the `media` section.
- **Render**: embed markup per kind; lens-hosted vs remote placeholder
  branches; the existing autoescape guard stays pinned.
- **Routes**: upload happy path / 413 / 400 bad type / origin 403;
  media serving headers, 404, token-shape rejection; CSP + nosniff
  header presence.
- **E2E**: upload → PRIVMSG with URL on the fake-IRCd wire → `chat`
  fragment contains the embed. Playwright: click-to-load swap, paste
  upload. Mic via chromium's
  `--use-fake-device-for-media-stream` / fake-UI flags.
- **Static**: `test_lens_js.py`-style grep tests for `media.js`; the
  lens.js line budget holds.

## Delivery phasing

| PR | Contents |
| --- | --- |
| 1 | Security headers + autolink + click-to-load rendering |
| 2 | Media store + upload/serve routes + console upload UI + config |
| 3 | Mic recording |
| — | File the two sibling-repo requests once PR 2 defines the URL shape |

Each PR: minor version bump, docs updates (`architecture.md`,
`sse-events.md` DOM contract, `security-checklist.md` for the
capability-URL model + CSP, `cli.md` config keys), and `learn` /
`explain` catalog mentions per the AFI rubric.

## Decision log

### Why URL-reference, not inline bytes

8192-byte inbound wall, no client-side chunking anywhere, IRCv3
batch/multiline explicitly deferred upstream, and the operator's
standing principle: links over the mesh, not binaries.

### Why capability URLs on `/media/`

Auth-gating media would mean only browser humans can view it — agents
have no CF/JWT identity, so the "agents see images" payoff would wait on
sibling-repo credential work. Unguessable-token URLs match the IRC trust
model (the URL is only ever disclosed inside a channel), uploads remain
authed, and CF Access still fronts everything in cloudflare-access mode.
Ratified 2026-07-02.

### Why click-to-load for remote media

Auto-embedding makes every viewer's browser dereference arbitrary hosts
posted by any channel member — an IP/header leak amplified in
cloudflare-access mode where multiple humans watch the same channel.
Click-to-load keeps the leak opt-in per user per item; `trusted_hosts`
restores auto-embed where the operator vouches.

### Why CSP lands with this feature

Media introduces the first `|safe`-adjacent markup injection and the
first user-controlled content serving. `script-src 'self'` +
`object-src 'none'` is the backstop if the escape-then-linkify pipeline
ever regresses.

### Why SVG is excluded

`<img src=*.svg>` is inert, but the same capability URL opened directly
executes the SVG's scripts on the lens origin — an auth-exempt stored
XSS. Raster-only until someone needs SVG enough to sandbox it.
