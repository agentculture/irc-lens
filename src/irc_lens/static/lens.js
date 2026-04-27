// irc-lens browser glue — Phase 4 placeholder.
// Phase 7 replaces this with the ≤50-line SSE→HTMX glue: append `chat`
// fragments to #chat-log, swap `roster`/`info` targets, toggle view
// classes on `view` events, render error toasts.

(function () {
  "use strict";
  // Use globalThis so this snippet stays valid in workers / non-window
  // contexts too. EventSource is browser-only, so the feature check still
  // matters; we just don't bind to `window` directly.
  if (typeof globalThis === "undefined" || !("EventSource" in globalThis)) return;
  const src = new EventSource("/events");
  src.addEventListener("chat",  (e) => console.log("[lens] chat",  e.data));
  src.addEventListener("roster",(e) => console.log("[lens] roster",e.data));
  src.addEventListener("info",  (e) => console.log("[lens] info",  e.data));
  src.addEventListener("view",  (e) => console.log("[lens] view",  e.data));
  src.addEventListener("error", (e) => console.warn("[lens] error",e.data));
  src.onerror = () => console.warn("[lens] SSE connection error");
})();
