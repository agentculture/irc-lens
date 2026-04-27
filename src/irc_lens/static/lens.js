// irc-lens browser glue. Wires SSE events to DOM swaps + clears the
// chat input on a successful POST. Kept small per the build plan;
// browser behaviour itself is exercised in Phase 9c via Playwright.
(function () {
  "use strict";
  if (typeof globalThis === "undefined" || !("EventSource" in globalThis)) return;

  // Cap on chat-log DOM nodes to bound long-session memory growth.
  // Mirrors the server-side `MessageBuffer` per-channel cap (500).
  const CHAT_LOG_CAP = 500;

  const $ = (id) => document.getElementById(id);
  const log = $("chat-log");
  const sidebar = $("sidebar");
  const info = $("info");
  const toasts = $("toast-region");
  const input = $("chat-input");
  const form = $("chat-form");

  function toast(message, kind) {
    if (!toasts) return;
    const tone = kind || "error";
    const isError = tone === "error";
    // Errors get assertive aria-live so screen readers announce them
    // immediately; info-level toasts stay polite.
    toasts.setAttribute("aria-live", isError ? "assertive" : "polite");
    const el = document.createElement("div");
    el.className = "lens-toast lens-toast--" + tone;
    el.setAttribute("role", isError ? "alert" : "status");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function appendChat(html) {
    if (!log) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    log.appendChild(tpl.content);
    // Trim oldest lines once the cap is exceeded so a long session
    // doesn't leak DOM. children is a live HTMLCollection — `length`
    // updates as we remove.
    while (log.children.length > CHAT_LOG_CAP) log.firstElementChild.remove();
    log.scrollTop = log.scrollHeight;
  }

  function swap(target, html) {
    if (target) target.innerHTML = html;
  }

  const src = new EventSource("/events");
  src.addEventListener("chat",   (e) => appendChat(e.data));
  src.addEventListener("roster", (e) => swap(sidebar, e.data));
  src.addEventListener("info",   (e) => swap(info, e.data));
  src.addEventListener("view",   (e) => {
    try { document.body.dataset.view = JSON.parse(e.data).view; }
    catch (err) { console.warn("[lens] bad view payload", err); }
  });
  src.addEventListener("error", (e) => {
    // EventSource fires `error` for transport-level failures too —
    // those carry no `data`. Only toast our app-level error events,
    // which always carry `{message: ...}` JSON. Transport failures
    // are surfaced via `src.onerror` below.
    if (typeof e.data !== "string" || !e.data) return;
    let msg = "error";
    try { msg = JSON.parse(e.data).message || msg; }
    catch (err) { console.warn("[lens] bad error payload", err); }
    toast(msg, "error");
  });
  src.onerror = () => { document.body.dataset.conn = "down"; };
  // Clear the down marker on (re-)connect so the UI doesn't stay
  // stuck after EventSource auto-reconnects.
  src.onopen = () => { delete document.body.dataset.conn; };

  // Clear input on 204; surface 4xx/5xx as a toast (HTMX swallows
  // non-2xx by default with `hx-swap="none"`).
  if (form && input) {
    form.addEventListener("htmx:afterRequest", (e) => {
      const xhr = e.detail?.xhr;
      if (!xhr) return;
      if (xhr.status === 204) { input.value = ""; return; }
      if (xhr.status >= 400) {
        let msg = "request failed (" + xhr.status + ")";
        try { msg = JSON.parse(xhr.responseText).error || msg; }
        catch (err) { console.warn("[lens] bad error response", err); }
        toast(msg, "error");
      }
    });
  }
})();
