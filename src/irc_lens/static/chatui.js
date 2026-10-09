// irc-lens chat UI glue (guest-mode uplift): the inline command palette,
// the phone rooms drawer, and header/room-title sync. Split out of lens.js
// to keep that file's line budget (see tests/test_lens_js.py). No SSE here:
// it reacts to DOM changes lens.js makes.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const input = $("chat-input");
  const form = $("chat-form");
  const sidebar = $("sidebar");

  // Keep the header + room title in step with the active room: the roster
  // fragment carries it as data-current on its first section.
  function syncRoom() {
    const cur = sidebar && sidebar.querySelector("[data-current]");
    if (!cur) return;
    const room = cur.getAttribute("data-current") || "";
    const head = $("header-room");
    const title = $("room-name");
    if (head) head.textContent = room;
    if (title) title.textContent = room || "No room";
  }
  if (sidebar) new MutationObserver(syncRoom).observe(sidebar, { childList: true });

  // --- Command palette (opens above the input when the text starts "/") ---
  const palette = $("cmd-palette");
  const rooms = document.querySelector('[data-testid="rooms-toggle"]');
  let active = -1;

  const rows = () => (palette ? Array.from(palette.querySelectorAll("li")) : []);
  const visibleRows = () => rows().filter((r) => !r.hidden);

  function setActive(idx) {
    const vis = visibleRows();
    rows().forEach((r) => r.setAttribute("aria-selected", "false"));
    active = vis.length && idx >= 0 ? idx % vis.length : -1;
    if (active >= 0) {
      vis[active].setAttribute("aria-selected", "true");
      vis[active].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", vis[active].id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function closePalette() {
    if (!palette) return;
    palette.hidden = true;
    active = -1;
    rows().forEach((r) => r.setAttribute("aria-selected", "false"));
    if (input) {
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    }
  }

  function refreshPalette() {
    if (!palette || !input) return;
    const v = input.value.toLowerCase();
    // Open only while typing the command word itself ("/" + letters).
    if (!v.startsWith("/") || /\s/.test(v)) { closePalette(); return; }
    let shown = 0;
    rows().forEach((r) => {
      const match = (r.dataset.cmd || "").startsWith(v);
      r.hidden = !match;
      if (match) shown += 1;
    });
    if (!shown) { closePalette(); return; }
    palette.hidden = false;
    input.setAttribute("aria-expanded", "true");
    setActive(-1);
  }

  function choose(row) {
    if (!row) return;
    if (row.dataset.href) { globalThis.location.assign(row.dataset.href); return; }
    input.value = (row.dataset.cmd || "") + " ";
    closePalette();
    input.focus();
  }

  if (palette && input) {
    input.addEventListener("input", refreshPalette);
    input.addEventListener("keydown", (e) => {
      if (palette.hidden) return;
      const vis = visibleRows();
      if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active <= 0 ? vis.length - 1 : active - 1); }
      else if (e.key === "Tab" && !e.shiftKey) {
        e.preventDefault();
        choose(vis[active >= 0 ? active : 0]);
      }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(vis[active]); }
      else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
    });
    // mousedown (not click) so the input keeps focus while a row is chosen.
    palette.addEventListener("mousedown", (e) => {
      const row = e.target.closest("li");
      if (!row) return;
      e.preventDefault();
      choose(row);
    });
    input.addEventListener("blur", closePalette);
  }

  // --- Phone: rooms drawer ---------------------------------------------
  function setNav(open) {
    document.body.classList.toggle("lens-nav-open", open);
    if (rooms) rooms.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (rooms) {
    rooms.addEventListener("click", () => setNav(!document.body.classList.contains("lens-nav-open")));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("lens-nav-open")) {
        setNav(false);
        rooms.focus();
      }
    });
    // Picking a room closes the drawer (the sidebar is re-rendered by SSE,
    // so listen on the stable container).
    if (sidebar) sidebar.addEventListener("click", (e) => {
      if (e.target.closest(".lens-channel")) setNav(false);
    });
  }

  // A successful send clears the input (lens.js); close the palette too.
  if (form) form.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail?.xhr?.status === 204) closePalette();
  });
})();
