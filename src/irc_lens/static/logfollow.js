// Chat-log scroll policy (exposes `globalThis.LensLog`).
//
// The log opens at the newest line and follows new lines only while the
// reader is already at the bottom -- never yank a reader who scrolled up.
// Lives apart from lens.js to keep that glue under its size budget.
(function () {
  "use strict";
  const SLOP_PX = 24;
  const log = document.getElementById("chat-log");
  let stuck = true; // first paint lands on the newest message

  function toBottom() {
    if (log) log.scrollTop = log.scrollHeight;
  }

  globalThis.LensLog = {
    // True when the reader is at (or near) the bottom right now.
    atBottom: () => stuck,
    // Scroll to the newest line and resume following (history swaps).
    pin() { stuck = true; toBottom(); },
    // Scroll only if the reader was following before a mutation.
    follow(wasStuck) { if (wasStuck) toBottom(); },
  };

  if (!log) return;
  log.addEventListener("scroll", () => {
    // A hidden log (mesh view) reports zero size; ignore those events.
    if (log.clientHeight === 0) return;
    stuck = log.scrollHeight - log.scrollTop - log.clientHeight <= SLOP_PX;
  }, { passive: true });
  toBottom();
  // Fonts / images / media cards settling after first paint grow the log.
  globalThis.addEventListener("load", () => { if (stuck) toBottom(); });
  // Returning from the mesh view (log was display:none) resets scrollTop.
  new MutationObserver(() => { if (stuck) toBottom(); })
    .observe(document.body, { attributes: true, attributeFilter: ["data-view"] });
})();
