# media support

> irc-lens ships image and audio support: media URLs render
> inline in the chat console (click-to-load for remote hosts),
> and the console can send media via file upload, drag-drop,
> paste, and mic recording — blobs hosted by the lens and
> shared as links over the mesh

## Audience

- humans using the lens console, and mesh agents that will
  fetch media URLs from channel messages (agent-side model
  input is the cultureagent follow-up)

## Before → After

- Before: chat is text-only everywhere: no media concept exists
  in irc-lens, agentirc, or cultureagent (verified in the
  2026-07-02 scope sweep — no attachment verb, no blob store,
  no unfurl logic)
- After: a channel member sees images and plays audio inline in
  the console, and a human can share a screenshot or voice note
  into a channel without leaving the lens

## Why it matters

- agents and humans in the mesh can collaborate on visual and
  audio artifacts (screenshots, charts, voice notes) instead of
  describing them in prose

## Requirements

- rendering: a new web/media.py does escape-first-then-linkify
  on chat text (http/https only) and classifies media URLs by
  extension allowlist; lens-hosted media embeds directly, remote
  media renders a click-to-load card per decision c3; ships in
  the existing chat/log SSE fragments unchanged
  - honesty: no raw user text ever passes through |safe —
    injected markup is built only from escaped components, and
    the pinned autoescape test (test_render.py chat-line XSS
    guard) stays green
- security headers land with rendering: CSP (script-src 'self',
  object-src 'none'), X-Content-Type-Options nosniff,
  Referrer-Policy no-referrer — the lens currently sets none
  (verified: no CSP anywhere in src/)
  - honesty: after the CSP lands the console still works: no
    inline scripts remain in index.html.j2 and
    htmx/sse/lens.js/media.js all load under script-src 'self'
- upload and serve: authed multipart POST /upload with
  magic-byte sniffing and size caps into a local blob store;
  auth-exempt GET /media/{token}.{ext} via FileResponse with
  capability tokens per decision c4
  - honesty: a GET without a valid token 404s; tokens are
    128-bit (secrets.token_urlsafe(16)); an uploaded file that
    fails the magic-byte sniff is rejected 400 with the {error,
    hint} shape
- console input UI: attach button, drag-drop,
  paste-from-clipboard, and mic recording (MediaRecorder to
  opus/webm) in a new static module media.js; on upload success
  the URL is sent through the existing /input pipeline (one send
  path)
  - honesty: there is exactly one send path: every media input
    (picker, drop, paste, mic) posts the returned URL through
    POST /input, so the wire message is an ordinary PRIVMSG
- config: a new optional media section (dir, max_file_bytes,
  max_store_bytes, public_base_url, remote_embeds,
  trusted_hosts) following the existing _validate_web_section
  loader pattern in config.py
  - honesty: a config file with no media section yields working
    defaults (feature on, XDG data dir, 10 MiB cap); afi cli
    verify and config init round-trip both pass

## Honesty conditions

- the announcement is honest only if both directions work
  end-to-end in the console: media URLs render (image embed
  visible, audio playable) AND all four input paths (picker,
  drag-drop, paste, mic) produce a playable/viewable message
  in-channel
- both constituencies are actually served in v1: humans get the
  console UX, and same-machine agents can fetch a posted media
  URL with no credentials (capability URL, no auth header)
- a Playwright run shows an image embed visible and an audio
  element playable inside the chat log, and a share initiated
  from the console lands as an ordinary channel message
- the sweep finding still holds at implementation time: no
  media/attachment/upload concept exists in agentirc,
  cultureagent, or irc-lens main branches before this feature
  lands
- sharing an artifact requires no tool outside the lens: no
  external image host is needed for a same-machine viewer to
  see it
- no code path in irc-lens sends media bytes on the IRC wire:
  posted messages carry only http(s) URLs and every generated
  message line stays under the 8192-byte inbound cap
- the upload allowlist excludes SVG and a crafted SVG upload is
  rejected 400 with {error, hint} — pinned by a test
- the proof is automated, not manual: the e2e chain, the
  autoescape guard, and afi cli verify all run in the CI test
  suite

## Success signals

- e2e proof: upload via POST /upload, the URL appears as a
  PRIVMSG on the fake-IRCd wire, and the chat fragment renders
  the embed; existing autoescape guard tests stay green; afi cli
  verify rubric still passes

## Scope / boundaries

- no inline media bytes on the IRC wire: agentirc rejects
  inbound lines over 8192 bytes (MAX_INBOUND_LINE, constants.py)
  and batch/multiline caps are explicitly deferred by agentirc's
  2026-07-01 accessibility spec
- no SVG uploads: an SVG served from the auth-exempt /media/
  route executes scripts on the lens origin when navigated
  directly — raster formats only (spec decision log)

## Non-goals

- no video and no web-page unfurling (og:image cards) in v1 —
  media detection is by file-extension allowlist only
- agent-side media consumption (backends receiving images) and
  protocol tag conventions are sibling-repo follow-ups
  (cultureagent, agentirc) drafted in the spec, not irc-lens v1
  work

## Assumptions

- same-machine agents can fetch lens media at the loopback URL
  today; cross-machine fetch requires the operator to set
  media.public_base_url (mesh-level reachability is the
  agentirc follow-up)

## Decisions

- v1 ships render and upload together, mic recording included
  (ratified 2026-07-02)
- remote-host media embeds are click-to-load; lens-hosted media
  auto-embeds; media.trusted_hosts widens auto-embed (ratified
  2026-07-02)
- GET /media/{token} uses auth-exempt 128-bit capability URLs
  like /static; POST /upload stays authed and origin-checked
  (ratified 2026-07-02)
- links over the mesh, not binaries: the IRC wire only ever
  carries short http(s) URLs; blobs move over HTTP (standing
  operator principle)

## Open / follow-up

- mesh-level cross-machine media reachability (advertised base
  URLs vs S2S relay) — agentirc spec proposal territory
