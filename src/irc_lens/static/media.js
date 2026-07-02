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
