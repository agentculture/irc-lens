// irc-lens click-to-load media embeds.
//
// Wires a delegated click listener on the #chat-log container to handle
// click-to-load media buttons. When a user clicks a lens-media-load button,
// the module reads data-src and data-kind, validates the URL, builds an
// img or audio element, and swaps it in place of the button.
(function () {
  "use strict";

  const log = document.getElementById("chat-log");
  if (!log) return; // Guard: safe on pages without chat-log

  log.addEventListener("click", function (e) {
    const button = e.target.closest(".lens-media-load");
    if (!button) return;

    // Read data-src and data-kind from button attributes
    const src = button.getAttribute("data-src");
    const kind = button.getAttribute("data-kind");

    if (!src || !kind) return;

    // Validate URL scheme: must be http:// or https://
    if (!src.startsWith("http://") && !src.startsWith("https://")) {
      return;
    }

    // Build the appropriate element based on kind
    let el;
    if (kind === "image") {
      el = document.createElement("img");
      el.setAttribute("src", src);
      el.setAttribute("loading", "lazy");
    } else if (kind === "audio") {
      el = document.createElement("audio");
      el.setAttribute("src", src);
      el.setAttribute("controls", "");
      el.setAttribute("preload", "metadata");
    } else {
      // Unknown kind: do nothing
      return;
    }

    // Set common attributes
    el.setAttribute("class", "lens-media-item");
    el.setAttribute("data-kind", kind);
    el.setAttribute("data-testid", "media-embed");

    // Replace button with element
    button.replaceWith(el);

    // Update wrapper's data-testid from media-placeholder to media-embed
    const wrapper = el.parentElement;
    if (wrapper && wrapper.classList.contains("lens-media")) {
      wrapper.setAttribute("data-testid", "media-embed");
    }
  });
})();

// irc-lens upload UI (task t8): attach button, drag-drop, paste, auto-send.
//
// All three input surfaces funnel through ONE send path: POST the file to
// /upload, and on 201 set the existing #chat-input value to the returned
// capability URL and call requestSubmit() on #chat-form — the exact same
// HTMX POST /input pipeline a typed message already uses. Never POST
// /input directly from here. Upload failures reuse lens.js's toast DOM
// pattern (#toast-region, .lens-toast, role="alert") via a tiny local
// helper — see that file for the canonical implementation this mirrors.
(function () {
  "use strict";

  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const chatArea = document.getElementById("chat-log");
  const attachButton = document.querySelector('[data-testid="media-attach"]');
  const fileInput = document.querySelector('[data-testid="media-file-input"]');

  function toast(message) {
    const toasts = document.getElementById("toast-region");
    if (!toasts) return;
    toasts.setAttribute("aria-live", "assertive");
    const el = document.createElement("div");
    el.className = "lens-toast lens-toast--error";
    el.setAttribute("role", "alert");
    el.textContent = message;
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Upload `file` to /upload; on 201, submit the returned URL through the
  // existing chat-input/#chat-form pipeline. On failure, parse the
  // {error, hint} body and toast it. Same-origin fetch, so the Origin
  // header the CSRF check requires is sent automatically by the browser.
  async function uploadAndSend(file) {
    if (!file || !form || !input) return;
    const body = new FormData();
    body.append("file", file);
    let resp;
    try {
      resp = await fetch("/upload", { method: "POST", body });
    } catch (err) {
      toast("upload failed: network error");
      return;
    }
    if (resp.status !== 201) {
      let payload = {};
      try {
        payload = await resp.json();
      } catch (err) {
        // Non-JSON error body — fall back to the status-only message.
      }
      let msg = payload.error || "upload failed (" + resp.status + ")";
      if (payload.hint) msg += " — " + payload.hint;
      toast(msg);
      return;
    }
    const data = await resp.json();
    input.value = data.url;
    form.requestSubmit();
  }

  // Attach button opens the hidden file picker; picking a file uploads it.
  if (attachButton && fileInput) {
    attachButton.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = ""; // reset so re-picking the same file still fires change
      if (file) uploadAndSend(file);
    });
  }

  // Drag-drop onto the chat log. preventDefault on dragover is required
  // for the browser to fire drop at all.
  if (chatArea) {
    chatArea.addEventListener("dragover", (e) => e.preventDefault());
    chatArea.addEventListener("drop", (e) => {
      e.preventDefault();
      const files = e.dataTransfer && e.dataTransfer.files;
      const file = files && files[0];
      if (file) uploadAndSend(file);
    });
  }

  // Paste an image while focused on the message input.
  if (input) {
    input.addEventListener("paste", (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === "file" && item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) {
            e.preventDefault();
            uploadAndSend(file);
          }
          break;
        }
      }
    });
  }
})();
