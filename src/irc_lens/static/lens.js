// irc-lens browser glue. Wires SSE events to DOM swaps + clears the
// chat input on a successful POST. Kept ≤ 60 lines per the build
// plan — anything bigger belongs in a separate module.
(function () {
  "use strict";
  if (typeof window === "undefined" || !("EventSource" in window)) return;

  const $ = (id) => document.getElementById(id);
  const log = $("chat-log");
  const sidebar = $("sidebar");
  const info = $("info");
  const toasts = $("toast-region");
  const input = $("chat-input");
  const form = $("chat-form");

  function toast(message, kind) {
    if (!toasts) return;
    const el = document.createElement("div");
    el.className = "lens-toast lens-toast--" + (kind || "error");
    el.setAttribute("role", "status");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function appendChat(html) {
    if (!log) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    log.appendChild(tpl.content);
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
    try { document.body.dataset.view = JSON.parse(e.data).view; } catch (_) {}
  });
  src.addEventListener("error",  (e) => {
    let msg = "error";
    try { msg = JSON.parse(e.data).message || msg; } catch (_) {}
    toast(msg, "error");
  });
  src.onerror = () => { document.body.dataset.conn = "down"; };

  // Clear input on 204; surface 503 / 5xx as a toast (HTMX swallows
  // non-2xx by default with `hx-swap="none"`).
  if (form && input) {
    form.addEventListener("htmx:afterRequest", (e) => {
      const xhr = e.detail && e.detail.xhr;
      if (!xhr) return;
      if (xhr.status === 204) { input.value = ""; return; }
      if (xhr.status >= 400) {
        let msg = "request failed (" + xhr.status + ")";
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (_) {}
        toast(msg, "error");
      }
    });
  }
})();
